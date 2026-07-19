---
name: rate
description: >
  Record a 1-5 rating for the current session — the flywheel's ground-truth
  "did this session actually go well?" metric. Use when the user says
  "/flywheel:rate", "rate this session", or gives a rating like "rate it a 4"
  or "that was a 5/5 session".
argument-hint: "[1-5] [optional note]"
---

# /flywheel:rate — rate this session

Human session ratings are the flywheel's most trusted outcome signal — more
trustworthy than any structural proxy. Record one honestly and cheaply.

## Step 1 — Get the rating

- If `$ARGUMENTS` starts with a number 1–5, use it (the rest is the note).
- Otherwise ask with AskUserQuestion (clickable), options:
  **"5 — excellent"** / **"4 — good"** / **"3 — okay"** / **"1–2 — rough"**
  (if they pick "1–2 — rough", record 2 unless they specify 1; "Other" free-text
  accepts anything). Ask exactly once; if they decline, drop it silently.

## Step 2 — Resolve the CURRENT session id

The live session's transcript is the most-recently-modified one for this
project directory:

```bash
SID=$(ls -t ~/.claude/projects/$(pwd | tr '/' '-')/*.jsonl 2>/dev/null | head -1)
SID=$(basename "$SID" .jsonl)
```

If that fails, still record the rating with `"session_id": ""` (ts+cwd lets the
dashboard join it approximately).

## Step 3 — Append the record

```bash
python3 - "$SID" <rating> "<note>" <<'PY'
import json, os, sys, time
p = os.path.expanduser("~/.claude/flywheel/state/ratings.jsonl")
os.makedirs(os.path.dirname(p), exist_ok=True)
rec = {"ts": int(time.time()), "session_id": sys.argv[1],
       "cwd": os.getcwd(), "rating": int(sys.argv[2]),
       "note": (sys.argv[3] if len(sys.argv) > 3 else "")[:200]}
open(p, "a").write(json.dumps(rec) + "\n")
print(f"recorded ★{rec['rating']}")
PY
```

Confirm in ONE short line ("Recorded ★4 for this session."). Do not editorialize
about the rating; a low score is valuable signal, not something to defend
against. Re-rating the same session is fine — the latest record wins.
