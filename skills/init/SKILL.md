---
name: init
description: >
  Set up claude-flywheel in the current project (one-time, ~30 seconds).
  Creates the project lesson store and config, verifies the hooks fire, and
  optionally adds the search-before-debugging habit line to CLAUDE.md. Use
  when: the user says "/flywheel:init", "set up the flywheel here", or right
  after installing the flywheel plugin in a new project.
---

# /flywheel:init — one-time project setup

Set up the current project for the flywheel. Idempotent: safe to re-run;
never overwrite existing files without asking.

## Step 1 — Scaffold the data directories

From the project root (the directory containing `.git`/`.claude`):

```bash
mkdir -p .claude/lessons
mkdir -p ~/.claude/flywheel/lessons ~/.claude/flywheel/state
```

Write `.claude/flywheel.json` **only if it doesn't exist**:

```json
{
  "version": 1,
  "injection": {
    "enabled": true,
    "maxInjections": 2,
    "minScore": 6,
    "minDistinct": 2
  }
}
```

## Step 2 — Verify the injection hook actually works

Self-test the hook exactly as the harness would invoke it. Find the plugin
root (the directory containing `hooks/inject.py` — for an installed plugin,
under `~/.claude/plugins/cache/claude-flywheel/`; when developing, the repo
itself), then:

```bash
echo '{"prompt":"flywheel init self-test — this arbitrary prompt should inject nothing","cwd":"'$PWD'","session_id":"init-selftest"}' \
  | python3 <plugin-root>/hooks/inject.py
```

Empty output = correct (no lessons match a nonsense prompt; the script ran
cleanly). A Python traceback or non-zero exit = broken — report it. If the
project already has lessons, also run a positive probe using one lesson's
keywords and confirm JSON with `additionalContext` comes back.

## Step 3 — Offer the habit line (ask first)

Ask the user (AskUserQuestion) whether to append this to the project's
CLAUDE.md (create the file if absent):

```markdown
- Before debugging an error or designing against a DB/API, search
  `.claude/lessons/` and `~/.claude/flywheel/lessons/` for the symptom
  (grep the keywords) — past incidents often already encode the fix.
```

This makes lesson-lookup a habit even when the automatic injection hook
doesn't fire (e.g., vague prompts).

## Step 4 — Seed offer (optional)

If the project has existing Claude transcripts
(`~/.claude/projects/<munged-cwd>/*.jsonl` where munged-cwd replaces `/` with
`-`), offer to run `/flywheel:learn --since 30d` now to seed the lesson store
from recent history.

## Step 5 — Report

Print a status block: lesson store path (+ how many lessons exist), config
path, hook self-test result, global tier path, and the three commands that
matter day-to-day:

```
/flywheel:learn          mine the latest session into lessons
/flywheel:learn --queued process everything the SessionEnd hook queued
/flywheel:consolidate    weekly: dedupe, promote, metrics
```
