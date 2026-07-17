#!/usr/bin/env python3
"""Tests for the injection matching upgrade (inject.py): synonyms, TF-IDF
rarity weighting, transcript-tail context, and the opt-in semantic rerank
staying a byte-for-byte no-op by default.

Run: python3 hooks/test_matching.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inject as I  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")


def L(lid, keywords, symptom="", body=""):
    return {"id": lid, "tier": "global", "mtime": I.time.time(),
            "meta": {"id": lid, "keywords": keywords, "symptom": symptom,
                     "class": "", "signal": "user-correction"},
            "body": body}


def main():
    print("synonym canonicalization (recall across distinct roots):")
    check("flaky -> intermittent", "intermittent" in I.tokens("the test is flaky"))
    check("sporadic -> intermittent", "intermittent" in I.tokens("failures are sporadic"))
    check("auth -> authentication", "authentication" in I.tokens("auth keeps failing"))
    check("deadlock -> race", "race" in I.tokens("we hit a deadlock"))

    print("\nTF-IDF rarity weighting (rare term outweighs common):")
    lessons = {
        "a": L("a", "alpha common shared"),
        "b": L("b", "beta common shared"),
        "c": L("c", "gamma common shared unique"),
    }
    idf = I.compute_idf(lessons)
    check("common term downweighted vs rare", idf.get("common", 9) < idf.get("alpha", 0),
          (idf.get("common"), idf.get("alpha")))
    check("idf normalized around 1", 0.3 < (sum(idf.values()) / len(idf)) < 1.7,
          sum(idf.values()) / len(idf))

    print("\npruned polysemous synonyms (precision guard):")
    check("'hidden' NOT mapped to blank", "blank" not in I.tokens("the advanced panel is hidden"))
    check("'stuck' NOT mapped to timeout", "timeout" not in I.tokens("i am stuck on this task"))
    check("'flaky' STILL maps to intermittent", "intermittent" in I.tokens("the test is flaky"))

    print("\nend-to-end: a synonym prompt matches a lesson keyworded differently:")
    sel = {"sel": L("sel", "intermittent, blank, selector, dropdown",
                    "dropdown missing sometimes")}
    pt = I.tokens("the lab picker is flaky and comes up blank")
    s = I.score_lesson(sel["sel"], pt, I.time.time(), I.compute_idf(sel))
    check("flaky+blank prompt hits intermittent/blank lesson",
          s["distinct"] >= 2 and s["strong"] >= 1, s)

    print("\nMIN_SCORE gate stays raw (idf only reorders rank):")
    raw = I.score_lesson(sel["sel"], pt, I.time.time(), None)
    wid = I.score_lesson(sel["sel"], pt, I.time.time(), I.compute_idf(sel))
    check("weighted_sum (the gate) unchanged by idf",
          raw["weighted_sum"] == wid["weighted_sum"], (raw["weighted_sum"], wid["weighted_sum"]))

    print("\ncontext union is anaphoric-gated (deictic marker required):")
    check("'why is it still blank?' is deictic", bool(I._DEICTIC.search("why is it still blank?")))
    check("self-contained prompt is NOT deictic",
          not I._DEICTIC.search("add a lab selector to the settings page"))

    print("\ntranscript-tail context (terse follow-up refers back to a pasted error):")
    d = tempfile.mkdtemp()
    tp = os.path.join(d, "t.jsonl")
    with open(tp, "w") as fh:
        fh.write(json.dumps({"message": {"content": "hey"}}) + "\n")
        fh.write(json.dumps({"message": {"content": "PGRST204 schema cache error on write"}}) + "\n")
    ct = I.recent_context_terms(tp)
    check("pulls 'schema' from the pasted error", "schema" in ct, ct)
    check("missing transcript -> empty set (fail-silent)",
          I.recent_context_terms("/no/such/file.jsonl") == set())

    print("\nsemantic rerank OFF by default = identity (zero-dep default intact):")
    cands = [(2.0, {"matched": []}, sel["sel"]), (1.0, {"matched": []}, sel["sel"])]
    check("no embeddings.json -> candidates unchanged",
          I.semantic_rerank(cands, "anything", "") == cands)

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
