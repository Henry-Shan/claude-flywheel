#!/usr/bin/env python3
"""claude-flywheel · UserPromptSubmit hook.

Matches the incoming user prompt against stored lessons and injects the
top-matching Strategy blocks as context — so the session meets a known
mistake BEFORE re-making it.

Lesson tiers searched (project overrides global on id collision):
  project: <project-root>/.claude/lessons/*.md
  global:  ~/.claude/flywheel/lessons/*.md

Design constraints:
  - stdlib only (plug-and-play: no pip installs)
  - fail silent + fast: a hook must never break or stall a session
  - silence over noise: strict match threshold, max N injections,
    per-session dedupe (a lesson is injected at most once per session)
"""

import json
import math
import os
import re
import sys
import time

# ---------------------------------------------------------------------------
# Tunables (project .claude/flywheel.json can override the first three)
# ---------------------------------------------------------------------------
MAX_INJECTIONS = 2
MIN_SCORE = 6          # minimum weighted match sum
MIN_DISTINCT = 2       # minimum distinct matched terms
MAX_STRATEGY_CHARS = 1200
MAX_LESSON_FILES = 400
MAX_LESSON_BYTES = 32_768
MAX_PROMPT_CHARS = 6000
RECENCY_HALF_LIFE_DAYS = 180
RECENCY_FLOOR = 0.4
STATE_DIR = os.path.expanduser("~/.claude/flywheel/state")
GLOBAL_LESSONS_DIR = os.path.expanduser("~/.claude/flywheel/lessons")

STOPWORDS = frozenset(
    """a an the and or but if then else when where how why what which who is are
    was were be been being do does did doing have has had having will would can
    could should shall may might must not no nor of in on at to from by for with
    about into over under again further once here there all any both each few
    more most other some such only own same so than too very just don doesn isn
    aren wasn weren won this that these those it its i me my we our you your he
    him his she her they them their as up out off down between through during
    before after above below because until while also still get got make made
    use using used want need see look please help fix issue problem error thing
    stuff work working code file line""".split()
)

_SUFFIXES = ("ing", "edly", "ed", "es", "s", "ly")


def stem(word: str) -> str:
    """Very light suffix-stripping stemmer — good enough for keyword overlap."""
    w = word
    for suf in _SUFFIXES:
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w


def tokens(text: str):
    """Lowercased, stemmed, stopword-filtered token set."""
    out = set()
    for raw in re.findall(r"[a-z0-9][a-z0-9'_-]+", (text or "").lower()):
        raw = raw.strip("'-_")
        if len(raw) < 3 or raw in STOPWORDS:
            continue
        out.add(stem(raw))
    return out


# ---------------------------------------------------------------------------
# Lesson loading
# ---------------------------------------------------------------------------

def find_project_root(cwd: str):
    """Ascend from cwd looking for a directory that owns a .claude/ or .git/."""
    path = os.path.abspath(cwd or os.getcwd())
    for _ in range(12):
        if os.path.isdir(os.path.join(path, ".claude")) or os.path.isdir(
            os.path.join(path, ".git")
        ):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def parse_frontmatter(text: str):
    """Parse a simple `key: value` YAML-ish frontmatter block. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("\"'")
        # strip trailing inline comments: `signal: user-correction  # note`
        value = re.split(r"\s+#", value)[0].strip()
        meta[key.strip()] = value
    return meta, text[end + 4 :]


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def extract_strategy(body: str) -> str:
    """Pull the **Strategy ...** block (up to the **Incident** section)."""
    match = re.search(
        r"\*\*Strategy[^\n]*\n?(.*?)(?=\n\s*\*\*Incident|\Z)", body, re.DOTALL
    )
    text = (match.group(1) if match else body).strip()
    # Re-attach the strategy heading's own trailing sentence if the bold header
    # held content on the same line: "**Strategy:** text..."
    header = re.search(r"\*\*Strategy[^*]*\*\*:?\s*([^\n]*)", body)
    if header and header.group(1).strip() and header.group(1).strip() not in text:
        text = header.group(1).strip() + "\n" + text
    if len(text) > MAX_STRATEGY_CHARS:
        text = text[:MAX_STRATEGY_CHARS].rsplit(" ", 1)[0] + " …"
    return text.strip()


def load_lessons(dirs):
    """Load lesson files from tier dirs (earlier dirs win id collisions)."""
    lessons = {}
    count = 0
    for tier, d in dirs:
        if not d or not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".md") or name.startswith("."):
                continue
            if count >= MAX_LESSON_FILES:
                return lessons
            path = os.path.join(d, name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(MAX_LESSON_BYTES)
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            count += 1
            meta, body = parse_frontmatter(text)
            lesson_id = meta.get("id") or os.path.splitext(name)[0]
            if lesson_id in lessons:  # project tier already provided it
                continue
            if meta.get("status", "active").lower() not in ("active", ""):
                continue
            lessons[lesson_id] = {
                "id": lesson_id,
                "tier": tier,
                "meta": meta,
                "body": body,
                "mtime": mtime,
            }
    return lessons


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def lesson_terms(lesson):
    """Build {term: weight} for a lesson. Higher weight = stronger trigger."""
    meta, body = lesson["meta"], lesson["body"]
    weights = {}

    def add(term_set, weight):
        for t in term_set:
            if weights.get(t, 0) < weight:
                weights[t] = weight

    add(tokens(meta.get("keywords", "")), 3)
    add(tokens(meta.get("symptom", "")), 3)
    add(tokens(meta.get("id", "") .replace("-", " ")), 2)
    add(tokens(meta.get("class", "").replace("-", " ")), 2)
    add(tokens(extract_strategy(body)), 1)
    return weights


def score_lesson(lesson, prompt_terms, now):
    weights = lesson_terms(lesson)
    matched = {t: w for t, w in weights.items() if t in prompt_terms}
    weighted_sum = sum(matched.values())
    distinct = len(matched)
    strong = sum(1 for w in matched.values() if w >= 3)
    meta = lesson["meta"]
    importance = 1.0 + math.log1p(
        max(
            0,
            to_int(meta.get("occurrences"), 1)
            + to_int(meta.get("helpful"))
            - 2 * to_int(meta.get("harmful")),
        )
    )
    age_days = max(0.0, (now - lesson["mtime"]) / 86400.0)
    recency = max(RECENCY_FLOOR, 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))
    return {
        "weighted_sum": weighted_sum,
        "distinct": distinct,
        "strong": strong,
        "rank": weighted_sum * importance * recency,
        "matched": sorted(matched),
    }


# ---------------------------------------------------------------------------
# Session state (dedupe + audit log)
# ---------------------------------------------------------------------------

def session_state_path(session_id: str):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")[:80]
    return os.path.join(STATE_DIR, f"injected-{safe}.json")


def load_injected(session_id):
    try:
        with open(session_state_path(session_id), "r", encoding="utf-8") as fh:
            return set(json.load(fh))
    except (OSError, ValueError):
        return set()


def save_injected(session_id, injected):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(session_state_path(session_id), "w", encoding="utf-8") as fh:
            json.dump(sorted(injected), fh)
    except OSError:
        pass


def log_injections(records):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(
            os.path.join(STATE_DIR, "injections.jsonl"), "a", encoding="utf-8"
        ) as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def cleanup_stale_state(now):
    """Occasionally drop per-session dedupe files older than 14 days."""
    try:
        if int(now) % 20 != 0:  # amortize: ~5% of invocations
            return
        for name in os.listdir(STATE_DIR):
            if not name.startswith("injected-"):
                continue
            path = os.path.join(STATE_DIR, name)
            if now - os.path.getmtime(path) > 14 * 86400:
                os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return

    prompt = (data.get("prompt") or "").strip()
    cwd = data.get("cwd") or os.getcwd()
    session_id = data.get("session_id") or "unknown"

    # Skip: trivial prompts, slash commands, and pure paste-continuations.
    if len(prompt) < 12 or prompt.startswith("/"):
        return

    project_root = find_project_root(cwd)

    # Per-project config overrides.
    max_inject, min_score, min_distinct = MAX_INJECTIONS, MIN_SCORE, MIN_DISTINCT
    enabled = True
    if project_root:
        try:
            with open(
                os.path.join(project_root, ".claude", "flywheel.json"),
                "r",
                encoding="utf-8",
            ) as fh:
                cfg = json.load(fh).get("injection", {})
            enabled = bool(cfg.get("enabled", True))
            max_inject = to_int(cfg.get("maxInjections"), MAX_INJECTIONS)
            min_score = to_int(cfg.get("minScore"), MIN_SCORE)
            min_distinct = to_int(cfg.get("minDistinct"), MIN_DISTINCT)
        except (OSError, ValueError):
            pass
    if not enabled:
        return

    dirs = []
    if project_root:
        dirs.append(("project", os.path.join(project_root, ".claude", "lessons")))
    dirs.append(("global", GLOBAL_LESSONS_DIR))

    lessons = load_lessons(dirs)
    if not lessons:
        return

    prompt_terms = tokens(prompt[:MAX_PROMPT_CHARS])
    if len(prompt_terms) < 2:
        return

    now = time.time()
    already = load_injected(session_id)

    candidates = []
    for lesson in lessons.values():
        if lesson["id"] in already:
            continue
        s = score_lesson(lesson, prompt_terms, now)
        if (
            s["weighted_sum"] >= min_score
            and s["distinct"] >= min_distinct
            and s["strong"] >= 1
        ):
            candidates.append((s["rank"], s, lesson))

    if not candidates:
        return

    candidates.sort(key=lambda c: -c[0])
    chosen = candidates[:max_inject]

    blocks = []
    audit = []
    for rank, s, lesson in chosen:
        strategy = extract_strategy(lesson["body"])
        if not strategy:
            continue
        meta = lesson["meta"]
        blocks.append(
            f"### Lesson: {lesson['id']}  (seen {to_int(meta.get('occurrences'), 1)}x, "
            f"tier: {lesson['tier']})\n{strategy}"
        )
        already.add(lesson["id"])
        audit.append(
            {
                "ts": int(now),
                "session": session_id,
                "lesson": lesson["id"],
                "tier": lesson["tier"],
                "rank": round(rank, 2),
                "matched": s["matched"][:12],
                "project": project_root or "",
            }
        )

    if not blocks:
        return

    save_injected(session_id, already)
    log_injections(audit)
    cleanup_stale_state(now)

    context = (
        "[flywheel] Past experience in this codebase/team matched this request. "
        "Apply these learned strategies if relevant (ignore if not; they are "
        "advisory, mined from real past sessions):\n\n" + "\n\n".join(blocks)
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — a hook must never break the session
        pass
    sys.exit(0)
