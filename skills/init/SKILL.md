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

## Step 2 — Verify the hooks actually work

Self-test both hook scripts exactly as the harness would invoke them. Find the
plugin root (the directory containing `hooks/inject.py` — for an installed
plugin it is under `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`,
e.g. `~/.claude/plugins/cache/claude-flywheel/flywheel/*/`; when developing,
the checked-out repo itself). Then, using a UNIQUE session id each run (the
per-session dedupe makes probes one-shot per session id):

```bash
SID="init-selftest-$(date +%s)"
printf '{"prompt":"flywheel init self-test arbitrary nonsense prompt","cwd":"%s","session_id":"%s"}' "$PWD" "$SID" \
  | python3 "<plugin-root>/hooks/inject.py"; echo "inject exit=$?"
printf '{"session_id":"%s","transcript_path":"","cwd":"%s"}' "$SID" "$PWD" \
  | python3 "<plugin-root>/hooks/outcome_log.py"; echo "outcome exit=$?"
```

Success criteria: **both exit codes are 0**, inject.py prints nothing for the
nonsense prompt, and no traceback appears. (Empty output alone is not enough —
check the exit code, since the scripts are fail-silent by design.) If the
project already has lessons, also run ONE positive probe using a lesson's
keywords (fresh `$SID`) and confirm JSON with `additionalContext` comes back.
Note: probes write a few benign records under `~/.claude/flywheel/state/`
(`injected-init-selftest-*.json`, a sessions.jsonl line) — harmless, and the
state cleaner ages them out.

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
