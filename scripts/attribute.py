#!/usr/bin/env python3
"""claude-flywheel · deterministic injection-outcome attribution.

The loop's outcome signal (helpful/harmful) must NOT hinge on a headless LLM
remembering to bump a counter in a prose skill — that path is fragile and, in
practice, never ran (no autopilot.log, empty events.jsonl, every counter 0 after
10 injections). This module makes attribution a deterministic CODE side effect.

For each lesson injected into a session (state/injections.jsonl records
{session, lesson, ts, matched, project}), it reads that session's transcript
*after* the injection timestamp and applies a structural rule:

  - a post-injection user INTERRUPTION, or a user CORRECTION whose text is
    topically related to the lesson        -> harmful  (we handed over the
        relevant strategy and the user still had to stop/redirect on-topic)
  - a clean continuation (>=1 post-injection user turn, no such correction)
                                            -> helpful
  - no post-injection user turn to judge    -> neutral (no bump)

It then increments that lesson's helpful:/harmful: frontmatter counter and
appends an events.jsonl record. Idempotent: each (session, lesson) pair is
attributed at most once (state/attributed.jsonl).

This is an ASSOCIATIONAL proxy — it can't prove the strategy was literally
followed, only that friction did/didn't recur on-topic after the advice landed.
It is labelled as such (`basis` on every event) and is deliberately conservative
in the harmful direction (topical gate). But it produces real, honest signal
from code on every injected session, independent of the LLM miner or autopilot.

Usage:
  python3 attribute.py session <session_id> [<transcript_path>]   # live (SessionEnd)
  python3 attribute.py backfill                                    # all injected sessions
stdlib only.
"""

import calendar
import contextlib
import json
import os
import sys
import tempfile
import time

try:
    import fcntl  # POSIX advisory locking
except ImportError:  # pragma: no cover — Windows: degrade to best-effort (no lock)
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics as M  # reuse CORRECTION, INTERRUPT, _human_text, _terms

HOME = os.path.expanduser("~")
FLY = os.path.join(HOME, ".claude", "flywheel")
STATE = os.path.join(FLY, "state")
GLOBAL_LESSONS = os.path.join(FLY, "lessons")
PROJECTS = os.path.join(HOME, ".claude", "projects")
INJECTIONS = os.path.join(STATE, "injections.jsonl")
EVENTS = os.path.join(STATE, "events.jsonl")
ATTRIBUTED = os.path.join(STATE, "attributed.jsonl")
LOCKFILE = os.path.join(STATE, "attribution.lock")


@contextlib.contextmanager
def _lock():
    """Exclusive cross-process lock around the whole attribute-and-bump section.
    SessionEnd spawns a SEPARATE detached attribute.py per session, and a single
    GLOBAL lesson is injected into many sessions — so without this, two runs
    read-modify-write the same lesson counter and lose increments (or double-
    attribute the same pair). Coarse is fine: attribution is cheap."""
    if fcntl is None:  # Windows fallback — no advisory lock available
        yield
        return
    os.makedirs(STATE, exist_ok=True)
    fh = open(LOCKFILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _epoch(t):
    if not t:
        return 0
    try:
        return calendar.timegm(time.strptime(str(t)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def _append(path, record):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


# --------------------------------------------------------------- transcript IO
def _munge(p):
    return p.replace("/", "-")


def transcript_for(session_id, project):
    """Best-effort resolve a session's transcript path from project cwd + id."""
    if project:
        cand = os.path.join(PROJECTS, _munge(project), session_id + ".jsonl")
        if os.path.exists(cand):
            return cand
    if os.path.isdir(PROJECTS):
        for d in os.listdir(PROJECTS):
            cand = os.path.join(PROJECTS, d, session_id + ".jsonl")
            if os.path.exists(cand):
                return cand
    return None


def _lesson_id_for(path):
    """Resolve a lesson file path to its id (frontmatter id, else filename stem)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        head = open(path, encoding="utf-8", errors="replace").read(2048)
        for ln in head.splitlines():
            s = ln.strip()
            if s.startswith("id:"):
                return s.partition(":")[2].strip().strip("\"'") or stem
    except OSError:
        pass
    return stem


def load_events(transcript_path):
    """Stream a transcript into (events, pulls):
      events = [(ts, is_human, text, has_interrupt)]  — for outcome classification
      pulls  = [(ts, lesson_id, lesson_path)]         — Read tool calls on lesson
               files (the PULL channel: the model deliberately retrieved a lesson)
    Returns (None, []) if unreadable, so the caller defers rather than mis-judges."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None, []
    events, pulls, pulled_ids = [], [], set()
    try:
        fh = open(transcript_path, encoding="utf-8", errors="replace")
    except OSError:
        return None, []
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            has_int = M.INTERRUPT in line
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            ts = _epoch(o.get("timestamp"))
            # pull detection: assistant Read of a lesson .md
            if o.get("type") == "assistant" and not o.get("isMeta"):
                for it in ((o.get("message") or {}).get("content") or []):
                    if not (isinstance(it, dict) and it.get("type") == "tool_use"
                            and it.get("name") == "Read"):
                        continue
                    fp = str((it.get("input") or {}).get("file_path") or "")
                    if fp.endswith(".md") and (
                            "/.claude/lessons/" in fp or "/flywheel/lessons/" in fp):
                        lid = _lesson_id_for(fp)
                        if lid not in pulled_ids:   # first pull per lesson counts
                            pulled_ids.add(lid)
                            pulls.append((ts, lid, fp))
            text = M._human_text(o)
            if text is not None or has_int:
                events.append((ts, text is not None, text or "", has_int))
    return events, pulls


# ------------------------------------------------------------------- classify
# Explicit success acknowledgements — the only positive signal we trust. "The
# session merely continued" is NOT evidence the strategy helped (that manufactures
# improvement, the exact failure the KPI redesign fought). We require the user to
# actually signal success, ON-TOPIC, and NOT negated. Word-boundaried so "resolved"
# doesn't fire inside "unresolved". Deliberately narrow — a false "helpful"
# over-promotes a lesson that did nothing.
import re as _re
POSITIVE_ACK = _re.compile(
    r"(\bthat works\b|\bthat worked\b|\bworks now\b|\bit works\b|\bworking now\b|"
    r"\bfixed it\b|\bis fixed\b|\bnow fixed\b|\bthat fixed\b|\bresolved\b|\bsolved\b|"
    r"\bthat did it\b|\bnailed it\b|\bperfect\b|\bexactly what\b|\bthat'?s it\b|"
    r"\bworks great\b|\bgood catch\b|\bnice catch\b)",
    _re.IGNORECASE,
)


def _is_success(text):
    """A positive ack that is NOT simultaneously a negation/complaint ("still not
    fixed", "doesn't work"). CORRECTION already captures the dissatisfaction
    forms, so its presence vetoes the ack."""
    return bool(POSITIVE_ACK.search(text)) and not M.CORRECTION.search(text)


def classify(events, inject_ts, lesson_terms):
    """Structural outcome of the session AFTER the injection.
    Returns (outcome, basis). lesson_terms = the injection's matched terms, used
    to keep BOTH signals topical (a generic ack/correction about an unrelated
    subtask must not credit/blame this lesson). Conservative by construction:
      harmful  = on-topic correction / interruption after the advice landed
      helpful  = an on-topic, non-negated success acknowledgement
      neutral  = everything else (the honest default — no bump)."""
    if events is None:
        return "neutral", "no-transcript"
    # only post-injection events with a real timestamp are judgeable
    post = [e for e in events if e[0] and e[0] >= inject_ts]
    interrupts = sum(1 for (_ts, _h, _t, hi) in post if hi)

    def topical(text):
        if not lesson_terms:
            return True
        low = text.lower()
        return any(len(t) >= 4 and t in low for t in lesson_terms)

    corrections = sum(
        1 for (_ts, is_h, text, _hi) in post
        if is_h and M.CORRECTION.search(text) and topical(text)
    )
    if interrupts:
        return "harmful", f"post-injection interruption x{interrupts}"
    if corrections:
        return "harmful", f"post-injection on-topic correction x{corrections}"
    positive = sum(
        1 for (_ts, is_h, text, _hi) in post
        if is_h and _is_success(text) and topical(text)
    )
    if positive:
        return "helpful", "post-injection on-topic success acknowledgement"
    return "neutral", "no explicit outcome signal post-injection"


# ------------------------------------------------------------- counter bumping
def find_lesson_file(lesson_id, project):
    dirs = []
    if project:
        dirs.append(os.path.join(project, ".claude", "lessons"))
    dirs.append(GLOBAL_LESSONS)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = os.path.join(d, name)
            try:
                head = open(path, encoding="utf-8", errors="replace").read(4096)
            except OSError:
                continue
            fid = None
            for ln in head.splitlines():
                s = ln.strip()
                if s.startswith("id:"):
                    fid = s.partition(":")[2].strip().strip("\"'")
                    break
            fid = fid or os.path.splitext(name)[0]
            if fid == lesson_id:
                return path
    return None


def bump_counter(path, field):
    """Increment an integer `field:` inside frontmatter (add it if absent).
    Frontmatter-only, atomic replace. Returns the new value or None."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fm, rest = text[:end], text[end:]
    new_val = None
    out_lines = []
    for ln in fm.splitlines():
        s = ln.strip()
        if new_val is None and s.startswith(field + ":") and ":" in s:
            try:
                cur = int(s.partition(":")[2].strip())
            except ValueError:
                cur = 0        # heal an empty/garbage counter to 0 rather than
                               # leaving it AND appending a duplicate key
            new_val = cur + 1
            out_lines.append(f"{field}: {new_val}")
        else:
            out_lines.append(ln)
    if new_val is None:  # field absent — append inside frontmatter
        new_val = 1
        out_lines.append(f"{field}: 1")
    try:
        # per-process-unique temp in the same dir → no shared-".tmp" clobber
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out_lines) + rest)
        os.replace(tmp, path)
        return new_val
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass
        return None


# ------------------------------------------------------------------- attribute
def _attributed_pairs():
    seen = set()
    for o in _read_jsonl(ATTRIBUTED):
        seen.add((o.get("session"), o.get("lesson")))
    return seen


def _keyword_terms(path):
    """Topical-gate terms for a pulled lesson: its curated keywords + symptom."""
    try:
        head = open(path, encoding="utf-8", errors="replace").read(4096)
    except OSError:
        return set()
    fields = []
    for ln in head.splitlines():
        s = ln.strip()
        if s.startswith(("keywords:", "symptom:")):
            fields.append(s.partition(":")[2])
    return set(M._terms(" ".join(fields)))


def attribute_session(session_id, transcript_path=None):
    """Attribute every not-yet-scored lesson USE in one session — both channels:
      pull (the model deliberately Read a lesson file; strongest evidence) and
      push (inject.py matched the prompt). Pulls are processed first, so when a
      lesson was both pushed and pulled the pull-basis attribution wins the
      (session, lesson)-once dedupe. Returns [(lesson, outcome), ...].

    The expensive, side-effect-free work (reading the transcript, classifying)
    runs OUTSIDE the lock; only the mutating tail (re-check seen, bump counters,
    append events) runs UNDER the cross-process lock, with `seen` re-read inside
    the lock so a concurrent run can't double-attribute the same pair."""
    injections = [r for r in _read_jsonl(INJECTIONS) if r.get("session") == session_id]
    project = next((r.get("project") for r in injections if r.get("project")), "") or ""
    tpath = transcript_path or transcript_for(session_id, project)
    events, pulls = load_events(tpath)  # read the transcript ONCE
    if not injections and not pulls:
        return []

    pending = []
    for ts, lid, fp in pulls:           # pulls FIRST — they win the dedupe
        outcome, basis = classify(events, ts, _keyword_terms(fp))
        if basis == "no-transcript":
            continue
        # a pull knows its exact file — no directory search needed
        pending.append((fp, lid, outcome, "pulled; " + basis, "pull"))
    for rec in injections:
        lid = rec.get("lesson")
        if not lid:
            continue
        outcome, basis = classify(events, rec.get("ts", 0), set(rec.get("matched") or []))
        if basis == "no-transcript":
            continue  # can't judge yet — leave unattributed for a later run
        pending.append((None, lid, outcome, basis, "push"))
    if not pending:
        return []

    applied = []
    with _lock():
        seen = _attributed_pairs()   # fresh read UNDER the lock
        for known_path, lid, outcome, basis, mode in pending:
            if (session_id, lid) in seen:
                continue
            ev = {"ts": int(time.time()), "op": "attribute", "auto": True,
                  "session": session_id, "lesson": lid, "mode": mode,
                  "outcome": outcome, "basis": basis}
            if outcome in ("helpful", "harmful"):
                f = (known_path if known_path and os.path.exists(known_path)
                     else find_lesson_file(lid, project))
                if f:
                    nv = bump_counter(f, outcome)
                    if nv is not None:
                        ev["bumped"] = f"{outcome}={nv}"
            _append(EVENTS, ev)
            _append(ATTRIBUTED, {"session": session_id, "lesson": lid, "outcome": outcome})
            seen.add((session_id, lid))
            applied.append((lid, outcome))
    return applied


def backfill():
    sessions = []
    for r in _read_jsonl(INJECTIONS):
        sid = r.get("session")
        if sid and sid not in sessions:
            sessions.append(sid)
    total = []
    for sid in sessions:
        total += attribute_session(sid)   # each call re-reads `seen` under lock
    return total


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "session":
        sid = sys.argv[2] if len(sys.argv) > 2 else ""
        tpath = sys.argv[3] if len(sys.argv) > 3 else None
        applied = attribute_session(sid, tpath) if sid else []
        print(f"attributed {len(applied)} injection(s) for {sid}: {applied}")
    elif len(sys.argv) >= 2 and sys.argv[1] == "backfill":
        applied = backfill()
        from collections import Counter
        c = Counter(o for _l, o in applied)
        print(f"attributed {len(applied)} injection(s): {dict(c)} -> {EVENTS}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
