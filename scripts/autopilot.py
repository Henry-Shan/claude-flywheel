#!/usr/bin/env python3
"""claude-flywheel · autopilot runner.

Spawned detached by the SessionEnd hook. Drains the mining queue and runs
periodic consolidation via a HEADLESS `claude -p` session — so the whole loop
(learn + consolidate) runs on its own, no human in the inner loop.

This is the only part of the flywheel that spends tokens on its own, so it is
**opt-in** (config `automation.enabled`) and heavily guarded:

  1. RECURSION: every spawned `claude -p` inherits FLYWHEEL_AUTOPILOT=1; the
     SessionEnd hook (outcome_log.py) sees it and does nothing — a mining
     session can't trigger more mining. This runner also refuses to start if
     that env var is already set.
  2. SINGLE-FLIGHT: a lockfile means only one runner is ever active.
  3. DEBOUNCE: mining runs at most once per `mineDebounceMinutes`, so a burst
     of short sessions collapses into one run (cost control).
  4. TIMEOUT: each headless run is wall-clock bounded; a hung run is killed.
  5. DRAIN-NOT-PER-SESSION: mining processes the whole queue at once.

Even if guard #1 ever failed, #2+#3+the queue-drain cap runaway spend at ~one
extra run. stdlib only; fully fail-silent.
"""

import json
import os
import shutil
import subprocess
import sys
import time

FLYWHEEL = os.path.expanduser("~/.claude/flywheel")
STATE = os.path.join(FLYWHEEL, "state")
GLOBAL_CFG = os.path.join(FLYWHEEL, "config.json")
LOCK = os.path.join(STATE, "autopilot.lock")
LOG = os.path.join(STATE, "autopilot.log")
LAST_MINE = os.path.join(STATE, "last-automine")
LAST_CONSOL = os.path.join(STATE, "last-autoconsolidate")
QUEUE = os.path.join(STATE, "pending-mine.jsonl")

DEFAULTS = {
    "enabled": False,
    "mineDebounceMinutes": 20,
    "consolidateEveryDays": 7,
    "runTimeoutSeconds": 900,
    "permissionMode": "scoped",   # "scoped" (allowlist) | "skip" (bypass all)
    "model": "",                  # optional model override, e.g. a cheaper one
}

# Tools the learn/consolidate skills legitimately need, for scoped mode.
ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "Write", "Edit",
    "Bash(grep:*)", "Bash(jq:*)", "Bash(ls:*)", "Bash(sed:*)", "Bash(awk:*)",
    "Bash(wc:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(find:*)",
    "Bash(mkdir:*)", "Bash(cp:*)", "Bash(python3:*)",
]


def log(msg):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        # keep the log bounded
        if os.path.getsize(LOG) > 256_000:
            with open(LOG, encoding="utf-8") as fh:
                tail = fh.readlines()[-800:]
            with open(LOG, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
    except OSError:
        pass


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(GLOBAL_CFG, encoding="utf-8") as fh:
            raw = json.load(fh)
        auto = raw.get("automation", {}) if isinstance(raw, dict) else {}
        if isinstance(auto, dict):
            for k in DEFAULTS:
                if k in auto:
                    cfg[k] = auto[k]
    except (OSError, ValueError):
        pass
    return cfg


def acquire_lock(timeout):
    try:
        if os.path.exists(LOCK):
            if time.time() - os.path.getmtime(LOCK) < timeout + 120:
                return False           # a live runner holds it
            os.remove(LOCK)            # stale — reclaim
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except (OSError, FileExistsError):
        return False


def release_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def has_unmined():
    try:
        with open(QUEUE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    if not json.loads(line).get("mined"):
                        return True
                except ValueError:
                    continue
    except OSError:
        pass
    return False


def minutes_since(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 60.0
    except OSError:
        return 1e12


def touch(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def queue_project_dirs(cap=6):
    """Distinct project roots of queued-unmined sessions — the miner writes
    project-tier lessons into <root>/.claude/lessons, which acceptEdits only
    auto-accepts if the directory was granted via --add-dir."""
    dirs = []
    try:
        with open(QUEUE, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("mined"):
                    continue
                d = r.get("cwd") or ""
                if d and os.path.isdir(d) and d not in dirs:
                    dirs.append(d)
                if len(dirs) >= cap:
                    break
    except OSError:
        pass
    return dirs


def run_claude(claude, prompt, cfg):
    env = dict(os.environ)
    env["FLYWHEEL_AUTOPILOT"] = "1"        # guard #1
    cmd = [claude, "-p", prompt]
    if cfg.get("permissionMode") == "skip":
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd += ["--permission-mode", "acceptEdits", "--allowed-tools", *ALLOWED_TOOLS]
        # Grant the write targets mining actually needs: the flywheel home
        # (lessons/state — cwd alone did not reliably cover it) and each queued
        # project's root (for project-tier lessons). This was THE blocker that
        # kept headless mining from ever landing a lesson file.
        for d in [FLYWHEEL] + queue_project_dirs():
            cmd += ["--add-dir", d]
    if cfg.get("model"):
        cmd += ["--model", str(cfg["model"])]
    log(f"START {prompt!r} ({cfg.get('permissionMode')})")
    try:
        with open(LOG, "a", encoding="utf-8") as out:
            subprocess.run(
                cmd, env=env, cwd=FLYWHEEL,
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                timeout=int(cfg["runTimeoutSeconds"]),
            )
        log(f"DONE  {prompt!r}")
        return True
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT {prompt!r}")
    except Exception as e:  # noqa: BLE001
        log(f"ERROR {prompt!r}: {e}")
    return False


def main():
    if os.environ.get("FLYWHEEL_AUTOPILOT"):    # guard #1: never recurse
        return
    cfg = load_cfg()
    if not cfg.get("enabled"):
        return
    claude = shutil.which("claude")
    if not claude:
        log("no `claude` binary on PATH — cannot autopilot")
        return
    if not acquire_lock(int(cfg["runTimeoutSeconds"])):   # guard #2
        return
    try:
        mine_due = has_unmined() and minutes_since(LAST_MINE) >= cfg["mineDebounceMinutes"]
        consol_due = minutes_since(LAST_CONSOL) >= cfg["consolidateEveryDays"] * 1440
        if mine_due:
            touch(LAST_MINE)                                  # guard #3
            run_claude(claude, "/flywheel:learn --queued", cfg)
        if consol_due:
            touch(LAST_CONSOL)
            run_claude(claude, "/flywheel:consolidate --auto", cfg)
    finally:
        release_lock()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — background runner must never surface a crash
        pass
    sys.exit(0)
