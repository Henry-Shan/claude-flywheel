#!/usr/bin/env python3
"""claude-flywheel · Stop-hook rating nudge.

After a SUBSTANTIAL session, show — exactly once — a one-line invitation to
rate the session (/flywheel:rate). Human ratings are the flywheel's ground-truth
outcome metric; everything else (friction, corrections, acks) is proxy.

Anti-annoyance contract, in order:
  1. never in headless/autopilot sessions
  2. never for small sessions (transcript < MIN_BYTES — quick Q&A isn't worth a nudge)
  3. never twice for the same session (marker file)
  4. never if the session is already rated
Fail-silent: any error → no nudge, never a broken turn.
"""

import json
import os
import re
import sys

STATE = os.path.expanduser("~/.claude/flywheel/state")
RATINGS = os.path.join(STATE, "ratings.jsonl")
MIN_BYTES = 30_000   # ≈ a session with real work in it


def _safe(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", sid or "")[:80]


def already_rated(sid):
    try:
        with open(RATINGS, encoding="utf-8") as fh:
            for line in fh:
                try:
                    if json.loads(line).get("session_id") == sid:
                        return True
                except ValueError:
                    continue
    except OSError:
        pass
    return False


def main():
    if os.environ.get("FLYWHEEL_AUTOPILOT"):
        return
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    sid = data.get("session_id") or ""
    tp = data.get("transcript_path") or ""
    if not sid or not tp:
        return
    try:
        if os.path.getsize(tp) < MIN_BYTES:
            return
    except OSError:
        return
    marker = os.path.join(STATE, f"rate-nudged-{_safe(sid)}")
    if os.path.exists(marker) or already_rated(sid):
        return
    try:
        os.makedirs(STATE, exist_ok=True)
        open(marker, "w").close()
    except OSError:
        return
    print(json.dumps({
        "systemMessage": "⭐ Optional: rate this session — /flywheel:rate (1–5). "
                         "Ratings are the flywheel's ground-truth metric."
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — a hook must never break the turn
        pass
    sys.exit(0)
