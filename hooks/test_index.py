#!/usr/bin/env python3
"""Tests for the SessionStart lesson index (session_index.py) — the pull channel.

Run: python3 hooks/test_index.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inject as I  # noqa: E402
import session_index as S  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")


def lesson_file(d, lid, symptom, status="active", helpful=0, harmful=0):
    p = os.path.join(d, lid + ".md")
    open(p, "w").write(
        f"---\nid: {lid}\nsymptom: \"{symptom}\"\nkeywords: \"x\"\n"
        f"status: {status}\nhelpful: {helpful}\nharmful: {harmful}\n---\n"
        "**Strategy:** do the thing.\n")
    return p


def main():
    d = tempfile.mkdtemp()
    lesson_file(d, "good-lesson", "the dropdown sometimes vanishes")
    lesson_file(d, "retired-lesson", "old problem", status="retired")
    lesson_file(d, "toxic-lesson", "misfiring one", helpful=0, harmful=3)
    saved = I.GLOBAL_LESSONS_DIR
    I.GLOBAL_LESSONS_DIR = d
    try:
        idx = S.build_index("/nonexistent-project-root")
        check("index built", bool(idx))
        check("active lesson listed", "good-lesson" in idx)
        check("symptom shown", "dropdown sometimes vanishes" in idx)
        check("file path shown (Read target)", d in idx)
        check("retired lesson excluded", "retired-lesson" not in idx)
        check("harmful-suppressed lesson excluded", "toxic-lesson" not in idx)
        check("usage-tracking notice present", "logged as usage" in idx)

        # empty dir -> no output at all
        empty = tempfile.mkdtemp()
        I.GLOBAL_LESSONS_DIR = empty
        check("no lessons -> no index", S.build_index("/nonexistent") is None)

        # autopilot guard: main() prints nothing
        I.GLOBAL_LESSONS_DIR = d
        os.environ["FLYWHEEL_AUTOPILOT"] = "1"
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO("{}")
        with contextlib.redirect_stdout(buf):
            S.main()
        sys.stdin = old
        del os.environ["FLYWHEEL_AUTOPILOT"]
        check("autopilot session -> silent", buf.getvalue() == "")

        # normal main() emits valid SessionStart hook JSON
        buf = io.StringIO()
        sys.stdin = io.StringIO("{}")
        with contextlib.redirect_stdout(buf):
            S.main()
        sys.stdin = old
        out = json.loads(buf.getvalue())
        check("valid hookSpecificOutput JSON",
              out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
              and "good-lesson" in out["hookSpecificOutput"]["additionalContext"])
    finally:
        I.GLOBAL_LESSONS_DIR = saved

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
