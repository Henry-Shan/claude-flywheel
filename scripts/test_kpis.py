#!/usr/bin/env python3
"""Validity tests for the difference-in-differences KPI (dashboard.kpis).

The whole point of the redesign: the metric must NOT manufacture improvement
for a lesson that did nothing. These tests encode that as executable claims.

Run: python3 scripts/test_kpis.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard as D  # noqa: E402

CUT = 1_000_000  # activation epoch
NOW = 2_000_000


def lesson(terms, created=CUT):
    return {"id": "L", "_terms": set(terms), "created": str(created)}


def sess(started, friction, terms):
    return {"resumed": False, "human_turns": 3, "started": started,
            "friction": friction, "terms": list(terms)}


MATCHED = ["foo", "bar", "baz", "x"]      # ≥3 overlap with the lesson's terms
UNCOVERED = ["qux", "zzz"]                 # 0 overlap → baseline cohort
LT = ["foo", "bar", "baz"]


def run(name, metrics, expect_verdict, expect_sign=None):
    k = D.kpis([lesson(LT)], metrics, injections=[], now=NOW)
    verdicts = k["verdicts"]
    got = len(verdicts) > 0
    ok = got == expect_verdict
    detail = ""
    if got:
        v = verdicts[0]
        detail = f"did={v['did']} (matchedΔ={v['delta_matched']} baselineΔ={v['delta_baseline']})"
        if expect_sign is not None:
            ok = ok and ((v["did"] < 0) == (expect_sign < 0))
    else:
        p = k["per_lesson"][0]
        detail = f"status={p['status']} n_matched={p['n_matched']} n b→a={p['matched_before']}→{p['matched_after']}"
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def before(n, f):   # n sessions just before activation
    return [sess(CUT - 1000 - i, f, MATCHED) for i in range(n)]


def after(n, f):
    return [sess(CUT + 1000 + i, f, MATCHED) for i in range(n)]


def ub(n, f):       # uncovered before
    return [sess(CUT - 1000 - i, f, UNCOVERED) for i in range(n)]


def ua(n, f):
    return [sess(CUT + 1000 + i, f, UNCOVERED) for i in range(n)]


def main():
    all_ok = True
    print("difference-in-differences validity:")

    # A. INERT lesson: matched tasks improve EXACTLY as much as the baseline
    #    trend. A naive before/after would scream "improved!"; DiD must not.
    inert = before(6, 10) + after(6, 6) + ub(6, 10) + ua(6, 6)
    all_ok &= run("inert lesson shows NO verdict (DiD≈0)", inert, expect_verdict=False)

    # B. GENUINE help: matched tasks fall MORE than the baseline trend.
    helps = before(6, 10) + after(6, 4) + ub(6, 10) + ua(6, 8)
    all_ok &= run("genuinely-helpful lesson shows a verdict (DiD<0)",
                  helps, expect_verdict=True, expect_sign=-1)

    # C. REGRESSION TO THE MEAN: the lesson was spawned by one catastrophic
    #    session (friction 50); every other matched session is flat at 5 before
    #    and after (the lesson did nothing). Trigger exclusion must drop the
    #    spike so no fake improvement appears.
    rtm = ([sess(CUT - 500, 50, MATCHED)] + before(5, 5)      # 6 pre incl. trigger
           + after(5, 5) + ub(5, 5) + ua(5, 5))
    all_ok &= run("regression-to-mean (trigger excluded) shows NO verdict",
                  rtm, expect_verdict=False)

    # D. INSUFFICIENT N: fewer than the gate on one side → no verdict, honest.
    thin = before(3, 10) + after(3, 4) + ub(3, 10) + ua(3, 8)
    all_ok &= run("thin data (n<gate) shows NO verdict", thin, expect_verdict=False)

    # E. NO ACTIVATION timestamp and no injection → excluded from causal KPI.
    k = D.kpis([{"id": "L", "_terms": set(LT), "created": ""}],
               before(6, 10) + after(6, 4) + ub(6, 10) + ua(6, 8),
               injections=[], now=NOW)
    e_ok = len(k["verdicts"]) == 0 and k["per_lesson"][0]["activated"] == 0
    print(f"  [{'PASS' if e_ok else 'FAIL'}] no activation stamp → excluded: "
          f"activated={k['per_lesson'][0]['activated']}")
    all_ok &= e_ok

    # F. Injection provides the fallback activation when frontmatter lacks one.
    k = D.kpis([{"id": "L", "_terms": set(LT), "created": ""}],
               before(6, 10) + after(6, 4) + ub(6, 10) + ua(6, 8),
               injections=[{"lesson": "L", "ts": CUT}], now=NOW)
    f_ok = len(k["verdicts"]) == 1
    print(f"  [{'PASS' if f_ok else 'FAIL'}] first-injection ts is the activation fallback: "
          f"verdicts={len(k['verdicts'])}")
    all_ok &= f_ok

    print("\nsafety (XSS / crash resistance):")
    # G. A lesson id that tries to break out of <script> must be neutralised in
    #    the static-embed path.
    evil = {"lessons": [{"id": "</script><img src=x onerror=alert(1)>"}],
            "kpis": {"per_lesson": [], "verdicts": []}, "totals": {}, "health": {}}
    out = D.render(evil)
    g_ok = "</script><img" not in out and "\\u003c/script" in out
    print(f"  [{'PASS' if g_ok else 'FAIL'}] </script> breakout is escaped in static embed")
    all_ok &= g_ok

    # H. read_jsonl must skip bare scalar / array lines instead of crashing the
    #    whole dashboard (one malformed metrics line shouldn't kill it).
    import tempfile
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "x.jsonl"), "w") as fh:
        fh.write('42\n"a string"\n[1,2]\n{"ok":1}\nnot json\n')
    saved = D.STATE
    D.STATE = d
    try:
        rows = D.read_jsonl("x.jsonl")
        h_ok = rows == [{"ok": 1}]
    finally:
        D.STATE = saved
    print(f"  [{'PASS' if h_ok else 'FAIL'}] read_jsonl skips non-dict lines: {rows}")
    all_ok &= h_ok

    print("\n" + ("ALL PASS" if all_ok else "FAILURES ABOVE"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
