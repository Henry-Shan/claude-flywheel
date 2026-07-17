---
name: status
description: >
  Show whether the flywheel is actually working — a health check plus a visual
  dashboard (lesson catalog, injection timeline, mining metrics). Use when the
  user says "/flywheel:status", "is the flywheel working", "flywheel
  dashboard", "show me the lessons", or wants to see what's been learned/injected.
argument-hint: "[--text | --open]"
---

# /flywheel:status — is it working?

Report the flywheel's health and generate its dashboard. Fast, read-only.

## Step 1 — Locate the dashboard script

It ships with the plugin. Find it (installed cache first, dev checkout as
fallback):

```bash
SCRIPT="$(ls -t ~/.claude/plugins/cache/claude-flywheel/flywheel/*/scripts/dashboard.py 2>/dev/null | head -1)"
[ -z "$SCRIPT" ] && SCRIPT="$(ls ~/claude-flywheel/scripts/dashboard.py 2>/dev/null | head -1)"
echo "using: $SCRIPT"
```

If neither exists, the plugin isn't installed or is an older version — tell the
user to run `/plugin install flywheel@claude-flywheel` (or
`/plugin marketplace update claude-flywheel` to upgrade), and stop.

## Step 2 — Health check + generate the dashboard

Run from the project root so the project's lesson tier is included:

```bash
python3 "$SCRIPT" --text --project "$PWD"     # health + metrics as text
python3 "$SCRIPT" --project "$PWD"            # (re)write the HTML dashboard
```

The HTML lands at `~/.claude/flywheel/dashboard.html`. If the user passed
`--open` (or asked to "open"/"show" it), also open it:

```bash
python3 "$SCRIPT" --project "$PWD" --open
```

## Step 3 — Report

Relay the health line and the key numbers to the user in chat: installed?
hooks runnable? how many lessons (by tier), how many injections (and when the
last one fired), helpful/harmful counters, mining-queue depth. Then give them
the dashboard path and offer to open it.

**Interpret honestly:**
- **0 injections on a fresh install is expected** — the hooks record activity
  as the user works, and only fire when a prompt's wording matches a lesson's
  symptom/keywords. It is NOT a sign of breakage. Say so.
- Newly-installed plugin hooks activate from the **next** Claude Code session,
  so injections won't appear until at least one session after install.
- If health shows a real failure (plugin not installed, hook won't compile),
  surface the specific fix from the health line.
- If there are lessons but the mining queue keeps growing with 0 mined,
  suggest `/flywheel:learn --queued`.
