#!/usr/bin/env python3
"""claude-flywheel · session metrics extractor.

Turns a session transcript into a structural metrics record — the raw signal
for answering "did the flywheel make Claude better?". These are CHEAP,
deterministic, LLM-free measures pulled straight from the JSONL. The miner
(/flywheel:learn) layers a few judged fields (difficulty, resolved,
mistake-classes) on top when it reads a session.

Records are appended to ~/.claude/flywheel/state/session-metrics.jsonl and
aggregated by the dashboard into the KPIs (see docs/METRICS.md).

Usage:
  python3 metrics.py extract <transcript.jsonl>      # print one JSON record
  python3 metrics.py backfill [--since 30d] [--project PATH]   # scan transcripts → session-metrics.jsonl
stdlib only.
"""

import argparse
import calendar
import glob
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".claude", "flywheel", "state")
METRICS_FILE = os.path.join(STATE, "session-metrics.jsonl")
PROJECTS = os.path.join(HOME, ".claude", "projects")

# Correction / friction signals, matched ONLY against genuine human text.
# Deliberately conservative — a false "correction" pollutes the friction KPI.
CORRECTION = re.compile(
    r"(that'?s not (right|what)|not what i|isn'?t what|you (broke|misunderstood)|"
    r"\brevert\b|\bundo\b|go back|that'?s wrong|is wrong|that'?s incorrect|not correct|"
    r"doesn'?t work|didn'?t work|still (not|broke|broken|doesn|don'?t|can'?t|isn'?t)|"
    r"instead of|\bno[,.]\s|\bnope\b|\bwrong,)",
    re.IGNORECASE,
)
PASTED_ERROR = re.compile(
    r"(Failed to load resource|Internal Server Error|HTTP\s*50\d|status\s*50\d|"
    r"blocked by CORS|Traceback|Unhandled|\bException\b|ERROR \[|PGRST\d|\b42[0-9]{3}\b|"
    r"is not defined|cannot read propert|undefined is not)",
    re.IGNORECASE,
)
INTERRUPT = "Request interrupted by user"
# Synthetic user-role messages that are NOT the human talking — harness
# injections, command output, notifications. Counting these as human turns
# inflated human_turns ~33% and polluted corrections/terms (review finding).
_SYNTHETIC_PREFIXES = (
    "[request interrupted", "<command-", "<local-command-stdout", "<command-name",
    "<task-notification", "<system-reminder", "caveat: the messages below",
    "[system notification", "this is an automated", "<user-prompt-submit-hook",
)

_STOP = frozenset(
    "the a an and or but if then when to of in on at for with is are was were be "
    "this that it its i we you he she they me my our your please can could would "
    "should do does did have has had not no so as up out get got make use fix "
    "issue problem error need want see look help thing code file line how why what "
    "which who now here there also still just".split()
)


def _terms(text, cap=60):
    """Distinct content tokens from a text blob — the session's 'topic
    fingerprint', used to match a session against a lesson's keywords."""
    out = []
    seen = set()
    for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower()):
        w = w.strip("-_")
        if len(w) < 3 or w in _STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= cap:
            break
    return out


def _human_text(o):
    """Return the human-typed text of a message, or None if it is not a
    genuine human turn — tool results, sidechain/subagent, meta, and synthetic
    harness messages (interrupt markers, command output, notifications) all
    excluded."""
    if o.get("type") != "user" or o.get("isSidechain") or o.get("isMeta"):
        return None
    if "toolUseResult" in o:
        return None
    msg = o.get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        if any(isinstance(i, dict) and i.get("type") == "tool_result" for i in c):
            return None
        text = " ".join(i.get("text", "") for i in c
                        if isinstance(i, dict) and i.get("type") == "text")
    else:
        return None
    text = (text or "").strip()
    if not text:
        return None
    low = text[:40].lower()
    if any(low.startswith(p) for p in _SYNTHETIC_PREFIXES):
        return None
    return text


def extract(path):
    human_turns = assistant_turns = tool_calls = tool_errors = 0
    interruptions = corrections = pasted_errors = 0
    files, skills = set(), set()
    first_ts = last_ts = None
    session_id = project = git_branch = ""
    model = ""
    human_blob = []
    friction_turns = 0

    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue

            session_id = session_id or o.get("sessionId") or o.get("session_id") or ""
            project = project or o.get("cwd") or ""
            git_branch = git_branch or o.get("gitBranch") or ""
            if o.get("attributionSkill"):
                skills.add(o["attributionSkill"])
            ts = o.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts

            typ = o.get("type")
            if typ == "assistant" and not o.get("isSidechain"):
                assistant_turns += 1
                msg = o.get("message") or {}
                model = model or msg.get("model") or ""
                for it in (msg.get("content") or []):
                    if isinstance(it, dict) and it.get("type") == "tool_use":
                        tool_calls += 1
                        name = it.get("name", "")
                        if name in ("Edit", "Write", "NotebookEdit"):
                            fp = (it.get("input") or {}).get("file_path")
                            if fp:
                                files.add(fp)
            elif typ == "user":
                if "toolUseResult" in o:
                    tr = o.get("toolUseResult")
                    if isinstance(tr, dict) and tr.get("is_error"):
                        tool_errors += 1
                    # tool_result content may also carry is_error
                    for it in ((o.get("message") or {}).get("content") or []):
                        if isinstance(it, dict) and it.get("is_error"):
                            tool_errors += 1
                    continue
                text = _human_text(o)
                if text is None:
                    continue
                human_turns += 1
                if len(human_blob) < 12000:
                    human_blob.append(text[:2000])
                is_corr = bool(CORRECTION.search(text))
                is_err = bool(PASTED_ERROR.search(text))
                if is_corr:
                    corrections += 1
                if is_err:
                    pasted_errors += 1
                if is_corr or is_err:
                    friction_turns += 1        # de-duped: one painful turn = one unit
            # interruptions are counted once per marker line (they arrive as
            # their own synthetic line, not inside a human turn)
            if INTERRUPT in line:
                interruptions += 1

    def epoch(t):
        if not t:
            return 0
        try:  # timestamps are UTC ('...Z'); timegm treats the struct as UTC
            return calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            return 0

    e0, e1 = epoch(first_ts), epoch(last_ts)
    dur_min = round((e1 - e0) / 60.0, 1) if e0 and e1 and e1 >= e0 else 0.0
    resumed = dur_min > 1440  # multi-day span ⇒ resumed/forked; duration unreliable

    # Friction: de-duped painful turns + interruptions + weighted tool errors.
    # A single turn contributes at most one "friction turn" (no double count).
    friction = friction_turns + interruptions + 0.5 * tool_errors

    return {
        "session_id": session_id or os.path.basename(path)[:-6],
        "transcript": os.path.basename(path),
        "project": os.path.basename(project) if project else "",
        "git_branch": git_branch,
        "model": model,
        "started": e0,
        "ended": e1,
        "duration_min": 0.0 if resumed else dur_min,
        "resumed": resumed,
        "human_turns": human_turns,       # ≈ rounds-to-resolution
        "assistant_turns": assistant_turns,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "interruptions": interruptions,
        "corrections": corrections,       # user redirects
        "pasted_errors": pasted_errors,   # user pasted a runtime error back
        "files_touched": len(files),
        "skills_used": sorted(skills),
        "friction": round(friction, 1),   # composite pain score (lower = smoother)
        "friction_rate": round(friction / max(1, human_turns), 2),  # per-round (length-normalized)
        "terms": _terms(" ".join(human_blob)),   # topic fingerprint for lesson matching
        "size": (os.path.getsize(path) if os.path.exists(path) else 0),
        "extracted_at": int(time.time()),
    }


def munged(project_path):
    return project_path.replace("/", "-")


def backfill(since_days=None, project=None):
    """Extract metrics for new/grown transcripts into session-metrics.jsonl.
    Dedup keys on (basename, size) so a resumed/grown session is re-measured
    rather than left stale; the newest record for a session wins at read time."""
    os.makedirs(STATE, exist_ok=True)
    seen = {}  # basename -> last recorded size
    if os.path.exists(METRICS_FILE):
        for line in open(METRICS_FILE, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
                seen[r.get("transcript")] = r.get("size", 0)
            except ValueError:
                continue
    if project:
        dirs = [os.path.join(PROJECTS, munged(os.path.abspath(project)))]
    else:
        dirs = [os.path.join(PROJECTS, d) for d in os.listdir(PROJECTS)] \
            if os.path.isdir(PROJECTS) else []
    cutoff = time.time() - since_days * 86400 if since_days else 0
    written = 0
    with open(METRICS_FILE, "a", encoding="utf-8") as out:
        for d in dirs:
            for path in glob.glob(os.path.join(d, "*.jsonl")):
                base = os.path.basename(path)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if base in seen and size <= seen[base]:  # unchanged since last run
                    continue
                if cutoff and os.path.getmtime(path) < cutoff:
                    continue
                rec = extract(path)
                if rec and (rec["human_turns"] > 0 or rec["assistant_turns"] > 0):
                    out.write(json.dumps(rec) + "\n")
                    written += 1
    return written


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    ex = sub.add_parser("extract")
    ex.add_argument("transcript")
    bf = sub.add_parser("backfill")
    bf.add_argument("--since", default=None)
    bf.add_argument("--project", default=None)
    args = ap.parse_args()

    if args.cmd == "extract":
        rec = extract(args.transcript)
        print(json.dumps(rec, indent=2) if rec else "null")
    elif args.cmd == "backfill":
        days = None
        if args.since:
            m = re.match(r"(\d+)d", args.since)
            days = int(m.group(1)) if m else int(args.since)
        n = backfill(days, args.project)
        print(f"metrics written for {n} new session(s) → {METRICS_FILE}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
