#!/usr/bin/env python3
"""Tests for the rating nudge (rate_nudge.py) and the dashboard rating stats.

Run: python3 hooks/test_rate.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rate_nudge as R  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")


def run_nudge(payload):
    buf = io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    with contextlib.redirect_stdout(buf):
        R.main()
    sys.stdin = old
    return buf.getvalue().strip()


def main():
    d = tempfile.mkdtemp()
    R.STATE = d
    R.RATINGS = os.path.join(d, "ratings.jsonl")

    big = os.path.join(d, "big.jsonl")
    open(big, "w").write("x" * 40_000)
    small = os.path.join(d, "small.jsonl")
    open(small, "w").write("x" * 500)

    print("rate_nudge:")
    out = run_nudge({"session_id": "s1", "transcript_path": big})
    check("substantial session -> nudge shown", "systemMessage" in out and "/flywheel:rate" in out)
    out = run_nudge({"session_id": "s1", "transcript_path": big})
    check("same session -> nudge only ONCE", out == "")
    out = run_nudge({"session_id": "s2", "transcript_path": small})
    check("small session -> no nudge", out == "")
    open(R.RATINGS, "w").write(json.dumps({"session_id": "s3", "rating": 4}) + "\n")
    out = run_nudge({"session_id": "s3", "transcript_path": big})
    check("already-rated session -> no nudge", out == "")
    os.environ["FLYWHEEL_AUTOPILOT"] = "1"
    out = run_nudge({"session_id": "s4", "transcript_path": big})
    del os.environ["FLYWHEEL_AUTOPILOT"]
    check("autopilot session -> no nudge", out == "")

    print("\ndashboard rating stats:")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
    import dashboard as D
    K = {"median_rounds": 1, "median_friction": 0, "friction_trend": []}
    ratings = ([{"session_id": f"u{i}", "rating": 5} for i in range(5)]     # used, avg 5
               + [{"session_id": f"n{i}", "rating": 3} for i in range(5)])  # not used, avg 3
    used = {f"u{i}" for i in range(5)}
    s = D._session_stats([], K, ratings=ratings, used_sessions=used)
    check("avg over all rated", s["rating_avg"] == 4.0, s["rating_avg"])
    check("rating_n counts sessions", s["rating_n"] == 10)
    check("lift = with − without (gated n≥5 both)", s["rating_lift"] == 2.0, s["rating_lift"])
    thin = D._session_stats([], K, ratings=ratings[:6], used_sessions=used)
    check("thin data -> lift withheld (None)", thin["rating_lift"] is None)
    rerate = D._session_stats([], K,
                              ratings=[{"session_id": "x", "rating": 2},
                                       {"session_id": "x", "rating": 5}],
                              used_sessions=set())
    check("re-rating: latest wins", rerate["rating_avg"] == 5.0, rerate["rating_avg"])

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
