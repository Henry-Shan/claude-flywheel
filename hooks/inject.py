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
# Tunables (project .claude/flywheel.json can override the injection ones)
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
LOG_MAX_BYTES = 512_000       # trim audit logs beyond this
LOG_KEEP_LINES = 1500
STATE_DIR = os.path.expanduser("~/.claude/flywheel/state")
GLOBAL_LESSONS_DIR = os.path.expanduser("~/.claude/flywheel/lessons")

# Evidence-quality multiplier: lessons backed by real outcome signals outrank
# self-judged ones (the design's "signal honesty" rule).
SIGNAL_WEIGHT = {
    "user-correction": 1.0,
    "ci-failure": 1.0,
    "reverted-pr": 1.0,
    "test-fail": 1.0,
    "self-judged": 0.7,
}

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

_SUFFIXES = ("ings", "ing", "edly", "ed", "es", "s", "ly")

# Synonym canonicalization — the free offline fix for the case pure stemming
# can't reach: distinct roots that mean the same symptom. Applied AFTER stem() so
# both a lesson's keywords and the incoming prompt collapse to the same token
# (e.g. prompt "flaky" and keyword "intermittent" now match). Keys/values are
# already stemmed forms. Keep entries symptom-oriented, not generic.
_SYNONYMS = {
    # symptom synonyms — distinct roots that name the SAME failure. Kept narrow:
    # polysemous/benign words (empty, hidden, stuck, freeze, random, gone) were
    # deliberately NOT mapped onto strong symptom keywords, to protect precision.
    "flaky": "intermittent", "flakey": "intermittent", "sporadic": "intermittent",
    "nondeterministic": "intermittent",
    "vanish": "disappear", "deadlock": "race",
    # abbreviation → canonical (low false-match risk)
    "creds": "credential", "cred": "credential",
    "auth": "authentication", "authn": "authentication",
    "perm": "permission", "rbac": "permission", "acl": "permission",
    "config": "configuration", "cfg": "configuration",
    "repro": "reproduce", "dupe": "duplicate", "dedup": "duplicate",
    "async": "asynchronous",
}


def stem(word: str) -> str:
    """Very light suffix-stripping stemmer — good enough for keyword overlap."""
    w = word
    for suf in _SUFFIXES:
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w


def tokens(text: str):
    """Lowercased, stemmed, synonym-canonicalized, stopword-filtered token set."""
    out = set()
    for raw in re.findall(r"[a-z0-9][a-z0-9'_-]+", (text or "").lower()):
        raw = raw.strip("'-_")
        if len(raw) < 3 or raw in STOPWORDS:
            continue
        t = stem(raw)
        out.add(_SYNONYMS.get(t, t))
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


def _clean_value(raw: str, strip_comment: bool) -> str:
    """Strip surrounding quotes; strip ' # comment' only for unquoted values
    (a '#' inside a quoted value — e.g. an issue number — is content)."""
    raw = raw.strip()
    was_quoted = raw[:1] in "\"'"
    value = raw.strip("\"'").strip()
    if strip_comment and not was_quoted:
        value = re.split(r"\s+#", value)[0].strip()
    return value


def parse_frontmatter(text: str):
    """Parse a simple `key: value` YAML-ish frontmatter block.

    Multi-line values are supported: an INDENTED line is treated as a
    continuation of the previous key's value and appended (this matches how
    the lesson schema wraps long `keywords:` lists). Top-level keys are never
    indented, so a continuation line containing a colon (e.g. wrapped prose)
    cannot clobber another key. Returns (meta, body).
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = {}
    last_key = None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        indented = line[:1] in (" ", "\t")
        stripped = line.strip()
        if indented and last_key is not None:
            # Continuation of the previous value (wrapped list/prose).
            fragment = _clean_value(stripped, strip_comment=False)
            if fragment:
                meta[last_key] = (meta.get(last_key, "") + " " + fragment).strip()
            continue
        if stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        meta[key] = _clean_value(value, strip_comment=True)
        last_key = key
    return meta, text[end + 4 :]


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


_STRATEGY_HEAD = re.compile(r"^(?:\*{1,2}|#{1,6}\s*)?\s*strategy\b", re.IGNORECASE)
_INCIDENT_HEAD = re.compile(r"^(?:\*{1,2}|#{1,6}\s*)?\s*incident\b", re.IGNORECASE)


def extract_strategy(body: str) -> str:
    """Pull the Strategy block (bold / heading / plain forms, case-insensitive).
    Never includes the Incident section; falls back to the first paragraph
    (pre-Incident) rather than ever returning the whole body."""
    out, in_strategy = [], False
    for line in body.splitlines():
        stripped = line.strip()
        if not in_strategy:
            if _STRATEGY_HEAD.match(stripped):
                in_strategy = True
                # Keep same-line content after the header's colon.
                _, sep, rest = stripped.partition(":")
                rest = rest.strip().lstrip("*").strip()
                if sep and rest:
                    out.append(rest)
            continue
        if _INCIDENT_HEAD.match(stripped):
            break
        out.append(line)
    text = "\n".join(out).strip()
    if not text:
        # Fallback: first paragraph, with any Incident section cut away first.
        pre_incident = []
        for line in body.splitlines():
            if _INCIDENT_HEAD.match(line.strip()):
                break
            pre_incident.append(line)
        text = "\n".join(pre_incident).strip().split("\n\n")[0].strip()
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
                "path": path,   # the pull channel needs a Read-able location
            }
    return lessons


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

# Terms that appear in virtually ANY software prompt. They may exist in a
# lesson's curated fields, but they must never COUNT as a match — audit of
# injections.jsonl showed unrelated lessons ranking 14-19 purely on words like
# "read/run/one/exact/user/data" (the flywheel's own mined lesson
# `injection-misfires-on-headless-sdk-runs` diagnosed exactly this).
_GENERIC = frozenset(
    """one two read run write call click open close exact skip full part item
    page list view time way sure even element user data state match rule assume
    paste confirm every instead built green feed take give put keep know done
    next also show need want change check press add remove""".split()
)


def lesson_terms(lesson):
    """Build {term: weight} for a lesson. Matching is restricted to the two
    CURATED retrieval fields — keywords (the designed trigger vocabulary) and
    symptom (user-language description). id/class/strategy-body terms are NOT
    match candidates: body text is full of ordinary coding words and was the
    main source of generic-token misfires."""
    meta = lesson["meta"]
    weights = {}

    def add(term_set, weight):
        for t in term_set:
            if weights.get(t, 0) < weight:
                weights[t] = weight

    add(tokens(meta.get("symptom", "")), 2)
    add(tokens(meta.get("keywords", "")), 3)   # keywords win on overlap
    return weights


def compute_idf(lessons):
    """Mean-normalized inverse document frequency over the lesson corpus. A term
    matched in MANY lessons (generic — 'read', 'run', 'file') is a weak trigger;
    one in FEW lessons ('swallowed', 'schema') is a strong discriminator. We scale
    each term's weight by idf so the discriminators dominate the score. Normalized
    to mean≈1 so the overall score scale — and therefore MIN_SCORE — is preserved;
    this reshapes ranking/gating without a blanket inflation."""
    df, n = {}, 0
    for lesson in lessons.values():
        n += 1
        for t in lesson_terms(lesson):        # distinct terms in this lesson
            df[t] = df.get(t, 0) + 1
    if not df:
        return {}
    raw = {t: math.log((n + 1) / (d + 1)) + 0.1 for t, d in df.items()}
    mean = sum(raw.values()) / len(raw)
    if mean <= 0:
        return {}
    return {t: v / mean for t, v in raw.items()}


def score_lesson(lesson, prompt_terms, now, idf=None):
    weights = lesson_terms(lesson)
    matched = {t: w for t, w in weights.items()
               if t in prompt_terms and t not in _GENERIC}
    # Gate on the RAW matched-weight sum so MIN_SCORE stays exactly calibrated
    # regardless of corpus size; apply idf ONLY to the rank used for ordering, so
    # rare discriminators sort ahead without moving the firing threshold.
    weighted_sum = sum(matched.values())
    if idf:
        rank_sum = sum(w * idf.get(t, 1.0) for t, w in matched.items())
    else:
        rank_sum = weighted_sum
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
    signal = (meta.get("signal") or "self-judged").strip().lower()
    signal_mult = SIGNAL_WEIGHT.get(signal, 0.85)
    age_days = max(0.0, (now - lesson["mtime"]) / 86400.0)
    recency = max(RECENCY_FLOOR, 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))
    return {
        "weighted_sum": weighted_sum,           # raw — for the MIN_SCORE gate
        "distinct": distinct,
        "strong": strong,
        "rank": rank_sum * importance * signal_mult * recency,   # idf-weighted
        "matched": sorted(matched),
    }


def suppressed_as_harmful(meta) -> bool:
    """A lesson repeatedly marked harmful stops injecting until /consolidate
    rehabilitates or retires it — 'harmful' must be able to bite."""
    harmful = to_int(meta.get("harmful"))
    helpful = to_int(meta.get("helpful"))
    return harmful >= 2 and harmful > helpful


# A prompt is "anaphoric" when it points back at prior context rather than
# standing on its own ("why?", "fix that", "same error", "it still fails"). Only
# then do we widen recall with the transcript tail — a self-contained prompt
# speaks for itself, and pulling stale context would hurt precision.
_DEICTIC = re.compile(
    r"\b(why|it|its|it's|that|this|these|those|there|them|again|same|still|"
    r"the error|the issue|the bug|fix that|do that|same error|same issue)\b",
    re.IGNORECASE,
)

# Prompts that are session management / tool plumbing, NOT about the code (no
# bug, no feature). Policy: never inject on these, so they also never pollute
# the injection log or helpful/harmful attribution. The canonical harmful case:
# "i connected to figma mcp, try again" pulled 4 unrelated lessons.
_ACK = re.compile(
    r"^(ok(ay)?|yes|yeah|yep|no|nope|thanks?( you)?|ty|nice|cool|great|perfect|"
    r"good( job)?|continue|go ahead|proceed|do it|next|done|stop|wait|hold on|"
    r"resume|keep going|carry on|sounds good|lgtm|approved?|try again|retry|again)"
    r"\b[\s\S]{0,60}$",
    re.IGNORECASE,
)
_META_HINT = re.compile(
    r"\b(try again|retry|reconnect|connected( to)?|installed|restart(ed)?|"
    r"reload(ed)?|logged? ?in|signed? ?in|log ?out|mcp|api key|token expired|"
    r"permissions?|rename|switch model)\b",
    re.IGNORECASE,
)
_CODE_HINT = re.compile(
    r"\b(bug|error|fix|broken|breaks?|fail(s|ed|ing)?|crash|feature|implement|"
    r"build|refactor|test|endpoint|component|function|button|screen|modal|"
    r"query|database|schema|api|deploy|500|404|undefined|null|exception)\b",
    re.IGNORECASE,
)


def looks_meta(prompt):
    """True for prompts that aren't about the code itself. Short conversational
    acks/continuations always skip; short tool/session-plumbing prompts skip
    unless they also carry a concrete code signal (bug/feature vocabulary)."""
    p = prompt.strip()
    if _ACK.match(p):
        return True
    return len(p) < 120 and bool(_META_HINT.search(p)) and not _CODE_HINT.search(p)


# Scripted persona prompts ("You are Echo, drafting iMessage replies…") are
# cron/SDK jobs, not a human typing — a fixed prompt an injected lesson cannot
# influence. The flywheel's own mined lesson (injection-misfires-on-headless-
# sdk-runs) diagnosed these; humans typing "you are wrong about X" don't match
# the <Name>, / <Name>: shape.
_SCRIPTED = re.compile(r"^\s*[Yy]ou are (an? )?[A-Z][\w-]*('s\b|\s*[,:*—])")  # persona shape: Name, / Name: / Name's


def looks_scripted(prompt):
    return bool(_SCRIPTED.match(prompt))


def is_sdk_session(transcript_path, sniff_bytes=4096):
    """Headless SDK/cron sessions self-identify in the transcript head
    (entrypoint: sdk-cli / promptSource: sdk). Never inject into them: the
    prompt is scripted, so injections are wasted tokens + polluted attribution."""
    if not transcript_path:
        return False
    try:
        with open(transcript_path, "rb") as fh:
            head = fh.read(sniff_bytes).decode("utf-8", "replace")
    except OSError:
        return False
    return ('"entrypoint":"sdk' in head or '"entrypoint": "sdk' in head
            or '"promptSource":"sdk"' in head or '"promptSource": "sdk"' in head)


_SYNTH_PREFIXES = (
    "[request interrupted", "<command-", "<local-command", "<task-notification",
    "<system-reminder", "caveat: the messages below", "[system notification",
)


def recent_context_terms(transcript_path, max_bytes=16384, max_lines=40):
    """Tokens from HUMAN turns in the transcript tail — catches an error the
    user pasted earlier that a terse follow-up ('why?', 'fix that') refers to.
    HUMAN TURNS ONLY: tool results / assistant output must never widen matching
    (the figma-session pileup came from tokenizing tool output full of coding
    vocabulary). Byte-bounded, fail-silent (any problem → empty set)."""
    if not transcript_path:
        return set()
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return set()
    out = set()
    for line in chunk.splitlines()[-max_lines:]:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict):   # a valid-JSON scalar/array line isn't a turn
            continue
        if o.get("type") != "user" or "toolUseResult" in o \
                or o.get("isSidechain") or o.get("isMeta"):
            continue
        content = (o.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            if any(isinstance(it, dict) and it.get("type") == "tool_result"
                   for it in content):
                continue
            text = " ".join(str(it.get("text") or "") for it in content
                            if isinstance(it, dict) and it.get("type") == "text")
        else:
            continue
        text = (text or "").strip()
        if not text or text[:40].lower().startswith(_SYNTH_PREFIXES):
            continue
        out |= tokens(text[:4000])
    return out


# --- Opt-in semantic rerank (default OFF; the stdlib path never touches this) --

def _cosine(a, b):
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    return dot / (da * db) if da > 0 and db > 0 else 0.0


def embed_prompt(prompt, cmd):
    """Run the user-configured embedder (text on stdin → JSON float vector on
    stdout). Absent/broken → None (rerank then no-ops)."""
    if not cmd:
        return None
    try:
        import subprocess
        r = subprocess.run(cmd, shell=True, input=prompt[:4000].encode("utf-8"),
                           capture_output=True, timeout=3)
        vec = json.loads(r.stdout.decode("utf-8"))
        return vec if isinstance(vec, list) else None
    except Exception:  # noqa: BLE001
        return None


def semantic_rerank(candidates, prompt, embedder_cmd):
    """OPT-IN. Blend cosine(prompt, cached lesson vector) into each candidate's
    rank as a light multiplier. Requires BOTH a write-time-cached vector store
    (state/embeddings.json = {lesson_id: [floats]}) AND an embedder command; any
    missing piece or error returns candidates UNCHANGED, so the zero-dependency
    default remains byte-for-byte identical and imports nothing new."""
    try:
        with open(os.path.join(STATE_DIR, "embeddings.json"), encoding="utf-8") as fh:
            vecs = json.load(fh)
        if not isinstance(vecs, dict) or not vecs:
            return candidates
        pv = embed_prompt(prompt, embedder_cmd)
        if not pv:
            return candidates
        out = []
        for rank, s, lesson in candidates:
            lv = vecs.get(lesson["id"])
            if isinstance(lv, list):
                rank = rank * (1.0 + 0.5 * max(0.0, _cosine(pv, lv)))
            out.append((rank, s, lesson))
        return out
    except Exception:  # noqa: BLE001 — a bad vector (TypeError in _cosine) must
        return candidates  # degrade to lexical rank, never abort the injection


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


def trim_log(path, max_bytes=LOG_MAX_BYTES, keep=LOG_KEEP_LINES):
    """Bound an append-only jsonl log. Atomic replace so concurrent appends
    lose at most a few lines rather than corrupting the file."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-keep:])
        os.replace(tmp, path)
    except OSError:
        pass


def log_prompt(session_id, prompt, now):
    """Log every REAL user message (it already passed the meta/scripted/SDK
    gates) — sessions solve several tasks, and the dashboard's activity feed
    should show them all, not only the messages that fired a lesson."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "prompts.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(now), "session": session_id,
                                 "prompt": prompt[:180]}) + "\n")
        trim_log(path)
    except OSError:
        pass


def log_injections(records):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "injections.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        trim_log(path)
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

    # Skip: headless flywheel runs (scripted prompts — injections can't change
    # their behavior and would pollute attribution), trivial prompts, slash
    # commands, and meta/session-management prompts that aren't about the code.
    if os.environ.get("FLYWHEEL_AUTOPILOT"):
        return
    if len(prompt) < 12 or prompt.startswith("/"):
        return
    if looks_meta(prompt) or looks_scripted(prompt):
        return
    if is_sdk_session(data.get("transcript_path")):
        return

    # This is a real human message about real work — record it for the activity
    # feed regardless of whether any lesson ends up matching below.
    log_prompt(session_id, prompt, time.time())

    project_root = find_project_root(cwd)

    # Per-project config overrides. A malformed/unexpected config must fall
    # back to defaults, never disable the feature silently.
    max_inject, min_score, min_distinct = MAX_INJECTIONS, MIN_SCORE, MIN_DISTINCT
    enabled = True
    semantic_on, embedder_cmd = False, ""
    if project_root:
        try:
            with open(
                os.path.join(project_root, ".claude", "flywheel.json"),
                "r",
                encoding="utf-8",
            ) as fh:
                raw_cfg = json.load(fh)
            cfg = raw_cfg.get("injection", {}) if isinstance(raw_cfg, dict) else {}
            if not isinstance(cfg, dict):
                cfg = {}
            enabled = bool(cfg.get("enabled", True))
            max_inject = to_int(cfg.get("maxInjections"), MAX_INJECTIONS)
            min_score = to_int(cfg.get("minScore"), MIN_SCORE)
            min_distinct = to_int(cfg.get("minDistinct"), MIN_DISTINCT)
            semantic_on = bool(cfg.get("semanticRerank", False))
            embedder_cmd = str(cfg.get("embedderCmd", "") or "")
        except (OSError, ValueError, TypeError, AttributeError):
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
    idf = compute_idf(lessons)   # corpus rarity weights (mean≈1)

    prompt_terms = tokens(prompt[:MAX_PROMPT_CHARS])
    if len(prompt_terms) < 2:
        return
    # Terse ANAPHORIC follow-ups ("why?", "fix that", "same error") refer back to
    # a pasted error/log; widen recall with the transcript tail. Gated on both a
    # deictic marker AND a short prompt so self-contained prompts never pull stale
    # context (precision guard).
    if len(prompt) < 120 and _DEICTIC.search(prompt):
        try:
            prompt_terms = prompt_terms | recent_context_terms(data.get("transcript_path"))
        except Exception:  # noqa: BLE001 — never let context-reading break injection
            pass

    now = time.time()
    already = load_injected(session_id)

    candidates = []
    for lesson in lessons.values():
        if lesson["id"] in already:
            continue
        if suppressed_as_harmful(lesson["meta"]):
            continue
        s = score_lesson(lesson, prompt_terms, now, idf)
        if (
            s["weighted_sum"] >= min_score
            and s["distinct"] >= min_distinct
            and s["strong"] >= 1
        ):
            candidates.append((s["rank"], s, lesson))

    if not candidates:
        return

    if semantic_on:   # opt-in; no-ops unless vectors + embedder are configured
        candidates = semantic_rerank(candidates, prompt, embedder_cmd)
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
                "prompt": prompt[:180],   # what the user typed — shown in Logs
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
    injected_ids = [a["lesson"] for a in audit]   # only what actually landed
    print(
        json.dumps(
            {
                # visible one-liner in the UI at the moment of injection, so the
                # user can SEE the flywheel working (and notice a misfire early)
                "systemMessage": "🎯 flywheel · lesson injected: " + ", ".join(injected_ids),
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
