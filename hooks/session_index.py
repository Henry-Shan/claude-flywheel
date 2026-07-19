#!/usr/bin/env python3
"""claude-flywheel · SessionStart lesson index — the PULL channel.

Push-injection (inject.py) guesses relevance from the user's first words — a
lexical proxy judging a semantic question at the wrong moment (the symptom
usually emerges MID-task). This hook flips the default: at session start it
injects a tiny INDEX of the lessons that exist (one symptom line each, like the
skill listing that Claude Code itself uses for skills), so the model can
recognize "this situation is that situation" while actually working, Read the
lesson file, and apply its Strategy.

A Read of a lesson file is a deterministic, transcript-visible event — the
SessionEnd attributor (scripts/attribute.py) scores pulls exactly like pushes,
and a *pulled* lesson followed by success is the strongest helpful-evidence the
flywheel can collect (the model chose it deliberately, in full context).

stdlib only, fail-silent, capped output. Never runs inside autopilot sessions.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inject as I  # reuse loaders: find_project_root, load_lessons, gates

MAX_LESSONS = 20        # index cap — at scale, only the top earners are listed
MAX_SYMPTOM = 140


def rank_key(lesson):
    """Most valuable first: proven-helpful, then most-recently touched."""
    meta = lesson["meta"]
    return (-I.to_int(meta.get("helpful")), -lesson["mtime"])


def build_index(cwd):
    root = I.find_project_root(cwd)
    dirs = []
    if root:
        dirs.append(("project", os.path.join(root, ".claude", "lessons")))
    dirs.append(("global", I.GLOBAL_LESSONS_DIR))
    lessons = [L for L in I.load_lessons(dirs).values()
               if not I.suppressed_as_harmful(L["meta"])]
    if not lessons:
        return None
    lessons.sort(key=rank_key)
    shown = lessons[:MAX_LESSONS]
    lines = []
    for L in shown:
        sym = " ".join((L["meta"].get("symptom", "") or "").split())[:MAX_SYMPTOM]
        lines.append(f'- {L["id"]} ({L["tier"]}): "{sym}"\n  file: {L["path"]}')
    more = len(lessons) - len(shown)
    return (
        f"[flywheel] Lesson index — {len(lessons)} mined lesson(s) exist for this "
        "environment. These are past mistakes/wins distilled into strategies. If the "
        "CURRENT work starts to match one of these symptoms (especially while "
        "debugging, or before proposing a fix), Read that lesson file and apply its "
        "**Strategy** section. Reading a lesson file is logged as usage and scored "
        "for outcome, so pull one only when you actually intend to apply it.\n"
        + "\n".join(lines)
        + (f"\n(+{more} more in the lesson dirs — grep them by symptom.)" if more > 0 else "")
    )


def main():
    if os.environ.get("FLYWHEEL_AUTOPILOT"):   # scripted runs can't use lessons
        return
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        data = {}
    context = build_index(data.get("cwd") or os.getcwd())
    if not context:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — a hook must never break session start
        pass
    sys.exit(0)
