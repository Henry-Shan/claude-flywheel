---
name: serve
description: >
  Start the live flywheel dashboard — a local web server (auto-refreshing) that
  shows health, the injection feed, the "did it make Claude better?" KPIs, and
  the lesson catalog. Use when the user says "/flywheel:serve", "start the
  flywheel dashboard", "run the dashboard server", or "live dashboard".
argument-hint: "[--port N]"
---

# /flywheel:serve — the live dashboard server

Start the dashboard as a local, auto-refreshing web server (vs. the static HTML
that `/flywheel:status` writes).

## Step 1 — Locate the script

```bash
SCRIPT="$(ls -t ~/.claude/plugins/cache/claude-flywheel/flywheel/*/scripts/dashboard.py 2>/dev/null | head -1)"
[ -z "$SCRIPT" ] && SCRIPT="$(ls ~/claude-flywheel/scripts/dashboard.py 2>/dev/null | head -1)"
```

If neither exists, the plugin isn't installed or is an older version — tell the
user to `/plugin install flywheel@claude-flywheel` (or
`/plugin marketplace update claude-flywheel`) and stop.

## Step 2 — Start it (background) and open it

The server runs until stopped, serving `http://127.0.0.1:8787` (it auto-picks a
free port if 8787 is busy). It re-reads state and refreshes the page every 5s.

Run it **detached** so it keeps serving after this turn, and open it:

```bash
nohup python3 "$SCRIPT" --serve --port 8787 --project "$PWD" >~/.claude/flywheel/state/server.log 2>&1 &
sleep 1 && sed -n '1p' ~/.claude/flywheel/state/server.log     # prints the live URL
open "http://127.0.0.1:8787" 2>/dev/null || xdg-open "http://127.0.0.1:8787" 2>/dev/null
```

Tell the user the URL, that it's live/auto-refreshing, and how to stop it
(`pkill -f 'dashboard.py --serve'`). If the KPI section reads "can't tell yet —
collecting data", explain it honestly: the "did it help?" number is a
difference-in-differences that needs ≥5 matched sessions both before and after a
lesson was activated, plus a baseline cohort — so it stays blank until there's
enough signal to not fool you, and fills in as they work. Meanwhile suggest a
history backfill: `python3 "$SCRIPT/../metrics.py" backfill --since 60d` (adjust
path).
