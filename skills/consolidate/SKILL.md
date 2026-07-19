---
name: consolidate
description: >
  Weekly lesson-store maintenance for the flywheel: dedupe and merge lessons,
  promote recurring ones up the ladder (memory → skill → CLAUDE.md rule),
  retire stale or harmful ones, and report learning metrics. Use when: the
  user says "/consolidate", "review the lessons", "is the flywheel working",
  or on a weekly cadence.
argument-hint: "[--dry-run]"
---

# /flywheel:consolidate — the promotion ladder + metrics

You are the consolidation stage of the claude-flywheel loop. You keep the
lesson store healthy and decide (with the human) what graduates upward.

**Iron rules**
1. **Delta operations only — never wholesale rewrites.** Every change to a
   lesson, skill, or CLAUDE.md is an explicit `ADD` / `UPDATE` / `DELETE` /
   `NOOP` on one entry. Full-file rewrites of evolving artifacts erode
   hard-won detail a little more each cycle (context collapse).
2. **A human ratifies every promotion.** Use AskUserQuestion for each
   promotion candidate. NEVER write to CLAUDE.md without explicit approval —
   it loads into every session; a bad rule poisons everything.
3. `--dry-run`: report everything you *would* do, change nothing.

### `--auto` mode (unattended, run by the autopilot)

When invoked with `--auto` (headless, from `scripts/autopilot.py`), you are
running with NO human present, so the "a human ratifies every promotion" rule
means: **do the automatic hygiene and metrics, but perform ZERO promotions.**

- **DO (safe, reversible):** dedupe/merge duplicate lessons, rescope
  project↔global, retire lessons where `harmful > helpful` or long-dead
  (status flip, never delete), fix counter integrity, and compute metrics.
- **DO NOT:** create skills, write to CLAUDE.md, or otherwise move anything up
  the ladder. There is no one to approve it, and auto-writing into always-on
  context is exactly the failure the design forbids.
- **Instead:** append every promotion candidate you *would* have proposed to
  `~/.claude/flywheel/state/promotion-candidates.md` (one section per
  candidate: lesson id, why it qualifies, the proposed skill/rule text). The
  human reviews that file and runs an interactive `/flywheel:consolidate` to
  approve. Also write the metrics report to
  `~/.claude/flywheel/state/consolidate-report.md`.
- Do not use AskUserQuestion in `--auto` mode (nothing can answer it).
- **Headless writes go to the OUTBOX** (your cwd, `~/.flywheel-outbox`) — never
  to `~/.claude/**` directly (blocked as sensitive; deterministic, don't retry).
  Lesson edits → rewrite the full file under `lessons/<id>.md` is NOT supported
  for edits (applier skips existing ids) — for hygiene edits, emit
  `bumps.jsonl` entries where possible and list anything else in
  `state/consolidate-report.md`; reports/candidates → `state/<name>`. The
  applier moves them into ~/.claude/flywheel/state after the run.

Interactive `/flywheel:consolidate` (no `--auto`) behaves as below — with the
human gate live.

## Step 1 — Inventory

Read both tiers: `<project>/.claude/lessons/*.md` and
`~/.claude/flywheel/lessons/*.md`. Parse frontmatter. Read the state files in
`~/.claude/flywheel/state/`: `injections.jsonl` (what was injected, when, into
which session), `events.jsonl` (the miner's add/bump operations — the source
for repeat-mistake and counter metrics), and `sessions.jsonl` (session
outcomes) since the last consolidation (track the cutoff in
`~/.claude/flywheel/state/last-consolidate`).

## Step 2 — Hygiene (automatic, report each op)

- **Dedupe/merge:** two lessons with the same root cause → merge into the
  stronger one (sum `occurrences`, union `keywords` and `sessions`, keep the
  better Strategy), `DELETE` the weaker. Same root cause = same corrective
  rule, not merely similar words.
- **Rescope:** a `project` lesson whose advice would hold in any codebase →
  move to the global tier (and vice versa).
- **Retire:** `status: retired` for lessons where `harmful > helpful`, or
  never injected/matched in 90+ days with `occurrences: 1` and no promotion.
  Retirement is an UPDATE (status flip), not deletion — history stays.
- **Counter integrity:** cross-check injections.jsonl — flag lessons injected
  often but never marked helpful (description/keywords may be misfiring).
- **Keyword self-tuning (the retrieval surface learns from outcomes):** join
  `injections.jsonl` (`matched` terms per firing) with `events.jsonl`
  (`op: attribute` outcomes, keyed by session+lesson). For each lesson:
  - a keyword that appears in `matched` across ≥5 firings whose outcomes are
    ONLY harmful/neutral — and never helpful and never in a `mode: pull`
    session — is a misfiring trigger: REMOVE it from `keywords:` (delta op,
    report it).
  - when a lesson was PULLED (`mode: pull`) and the outcome was helpful, look at
    the words the user/agent actually used for the problem in that session and
    ADD the 1–3 strongest symptom-shaped terms missing from `keywords:`.
  Never touch the Strategy text in this step, and never edit `helpful:`/
  `harmful:` (owned by the attributor).

## Step 3 — Promotion candidates (human-gated, one question each)

Work the ladder — propose, don't impose:

| candidate | condition | action on approval |
|---|---|---|
| **→ skill** | procedural AND **evidenced to work when applied**: `helpful ≥ 1`, or ≥2 independent sessions where following the strategy demonstrably resolved the issue. Recurrence alone (`occurrences`) is NOT evidence — a mistake seen twice proves the problem recurs, not that the runbook works. | Scaffold a skill (project `.claude/skills/<id>/SKILL.md`, or `~/.claude/skills/` if global): symptom-first description for auto-triggering, runbook body distilled from the lesson + incidents. Lesson gets `status: promoted` + pointer. |
| **→ CLAUDE.md rule** | a *class* of lesson keeps recurring across ids (e.g. three `skipped-ground-truth` lessons) | Propose ONE short behavioral line for the project's CLAUDE.md capturing the class ("verify against the live store before writing queries"). Show exact wording; append only after approval. Budget: keep learned rules ≤ ~10 lines total — if at budget, propose a swap, not an add. |
| **→ memory** | a durable *fact* (not procedure) the harness should recall | Write it to the project's auto-memory directory following its existing format, and add the one-line index entry. |

## Step 4 — Metrics report

End with the scoreboard (this is how we know the flywheel is real):

- **Repeat-mistake rate:** lessons whose `occurrences` grew this period (the
  number that should trend DOWN over time).
- **Injection hit-rate:** injections this period; how many were followed by a
  `helpful` bump vs ignored.
- **Coverage:** lessons by class and scope; newest lessons.
- **Dead weight:** lessons retired; lessons misfiring.
- **Verdict:** one honest line — is the loop learning, or decorative? If
  repeat-mistake rate hasn't fallen after ~4 consolidations, say so and
  recommend fixing the miner/retrieval rather than continuing on autopilot.

Finally, update `~/.claude/flywheel/state/last-consolidate` with the current
timestamp.
