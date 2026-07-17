#!/usr/bin/env python3
"""claude-flywheel · SessionStart update check.

Claude Code pins a plugin to its install-time version; third-party marketplaces
default to auto-update OFF, so users silently run stale code (this plugin itself
was found pinned at 0.1.0 while 0.4.0 shipped). This hook closes that gap the
safe way: on session start it checks whether a newer flywheel version is
published on GitHub and, if so, prints a one-line notice with the update command.

It NEVER modifies ~/.claude/plugins/cache/... in place — doing so desyncs Claude
Code's version tracking (installed_plugins.json). It only NOTIFIES; the built-in
`/plugin update` (or marketplace auto-update, if the user enables it once) does
the actual update. stdlib only (urllib), fail-silent, and network-checked at
most once per day so it never taxes session start.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.request

_VER_RE = re.compile(r"^\d+(\.\d+)*$")

STATE = os.path.expanduser("~/.claude/flywheel/state")
CACHE = os.path.join(STATE, "update-check.json")
# the plugin's own manifest on the default branch — the version users would get
RAW_MANIFEST = ("https://raw.githubusercontent.com/Henry-Shan/claude-flywheel/"
                "main/.claude-plugin/plugin.json")
CHECK_EVERY = 86400   # re-hit the network at most once per day
TIMEOUT = 3           # seconds; a slow/absent network must not stall startup


def _ver(s):
    """Parse a dotted version into a comparable int tuple; junk → 0."""
    out = []
    for part in str(s or "").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def installed_version():
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..")
    for rel in (".claude-plugin/plugin.json", "plugin.json"):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                return json.load(fh).get("version", "")
        except (OSError, ValueError):
            continue
    return ""


def _fetch_latest():
    """Fetch the published version in a DAEMON THREAD joined with a hard
    wall-clock cap — urllib's timeout does not bound DNS resolution, so a hung
    resolver could otherwise stall session start up to the hook's 10s ceiling.
    Returns a shape-valid version string, or '' on any failure."""
    box = {}

    def go():
        try:
            req = urllib.request.Request(
                RAW_MANIFEST, headers={"User-Agent": "flywheel-update-check"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                box["v"] = json.loads(r.read(65536).decode("utf-8")).get("version", "")
        except Exception:  # noqa: BLE001 — offline / rate-limited / DNS: stay silent
            pass

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(TIMEOUT + 1)          # abandon the daemon if the socket/DNS still hangs
    v = box.get("v", "")
    return v if isinstance(v, str) and _VER_RE.match(v) else ""


def latest_version():
    now = time.time()
    try:  # serve a SHAPE-VALID cached value unless it's older than a day
        with open(CACHE, encoding="utf-8") as fh:
            c = json.load(fh)
        v = c.get("latest", "")
        if now - c.get("ts", 0) < CHECK_EVERY and isinstance(v, str) and _VER_RE.match(v):
            return v
    except (OSError, ValueError):
        pass
    latest = _fetch_latest()
    if latest:   # persist ONLY a successful, shape-valid fetch — a failed '' must
        try:     # not get cached and suppress checks for the next 24h
            os.makedirs(STATE, exist_ok=True)
            with open(CACHE, "w", encoding="utf-8") as fh:
                json.dump({"ts": now, "latest": latest}, fh)
        except OSError:
            pass
    return latest


def main():
    if os.environ.get("FLYWHEEL_AUTOPILOT"):   # never nag inside headless runs
        return
    try:
        json.load(sys.stdin)   # consume the SessionStart payload (unused)
    except (ValueError, OSError):
        pass
    inst, latest = installed_version(), latest_version()
    if inst and latest and _ver(latest) > _ver(inst):
        msg = (f"🎯 flywheel {latest} is available (installed: {inst}). "
               f"Update with  /plugin update flywheel@claude-flywheel  — or enable "
               f"auto-update for the claude-flywheel marketplace in /plugin so it "
               f"stays current on its own.")
        print(json.dumps({"systemMessage": msg}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — a hook must never break session start
        pass
    sys.exit(0)
