#!/usr/bin/env python3
"""claude-flywheel · SessionEnd hook.

Appends a one-line outcome record per session and queues the session for
mining (`/flywheel:learn --queued`). This is the capture layer: cheap
metadata that makes later mining targeted instead of a 342MB archaeology dig.

Fail-silent by design: a hook must never break or stall session teardown.
"""

import json
import os
import sys
import time

STATE_DIR = os.path.expanduser("~/.claude/flywheel/state")
SESSIONS_LOG = os.path.join(STATE_DIR, "sessions.jsonl")
MINE_QUEUE = os.path.join(STATE_DIR, "pending-mine.jsonl")
MIN_TRANSCRIPT_BYTES = 20_000  # skip trivial sessions — nothing to mine
MAX_QUEUE_LINES = 500


def append_line(path, record):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def queued_session_ids():
    try:
        with open(MINE_QUEUE, "r", encoding="utf-8") as fh:
            ids = set()
            for line in fh:
                try:
                    ids.add(json.loads(line).get("session_id"))
                except ValueError:
                    continue
            return ids
    except OSError:
        return set()


def trim_file(path, max_lines):
    """Keep an append-only file bounded — oldest entries fall off. Uses an
    atomic replace so a concurrent writer can't observe a torn file (at worst
    a few concurrent appends are lost, never corrupted)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) > max_lines:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-max_lines:])
            os.replace(tmp, path)
    except OSError:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return

    session_id = data.get("session_id") or "unknown"
    transcript_path = data.get("transcript_path") or ""
    cwd = data.get("cwd") or ""
    reason = data.get("reason") or ""  # not guaranteed by all versions

    os.makedirs(STATE_DIR, exist_ok=True)

    record = {
        "ts": int(time.time()),
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "reason": reason,
    }
    append_line(SESSIONS_LOG, record)
    trim_file(SESSIONS_LOG, 3000)

    # Queue for mining if the transcript is substantial and not already queued.
    try:
        size = os.path.getsize(transcript_path) if transcript_path else 0
    except OSError:
        size = 0
    if size >= MIN_TRANSCRIPT_BYTES and session_id not in queued_session_ids():
        record = dict(record, transcript_bytes=size, mined=False)
        append_line(MINE_QUEUE, record)
        trim_file(MINE_QUEUE, MAX_QUEUE_LINES)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — never break session teardown
        pass
    sys.exit(0)
