# Measuring whether the flywheel actually improved Claude

The trap: counting *activity* ("N lessons, N injections") proves the machine
runs, not that it **helps**. Proving help needs a **comparison** — the same kind
of task, with vs without the learned lesson. Everything below is built around
that.

## The full metric catalog (mined from real transcripts)

Grouped by what they measure. ✓ = extracted structurally today (cheap,
LLM-free, in `scripts/metrics.py`); ◐ = needs a judged pass by the miner.

**Effort / friction — how painful was the task**
- ✓ **rounds** (`human_turns`) — user↔assistant cycles to resolution
- ✓ **corrections** — user turns that redirect ("no, actually", "that's wrong")
- ✓ **interruptions** — `[Request interrupted by user]` markers
- ✓ **pasted_errors** — user pasted a runtime error back (a failure escaped to them)
- ✓ **tool_errors** — failed tool calls (`is_error`)
- ✓ **tool_calls**, **files_touched**, **duration** — raw work/breadth/cost proxies
- ✓ **friction** — composite: `friction_turns + interruptions + 0.5·tool_errors`,
  where a *friction turn* is one human turn that either corrected or pasted an
  error (counted once even if it did both — no double-count, no length-scaling)
- ◐ **U-turns / wrong directions** — approaches proposed then reversed
- ◐ **rework ratio** — edits later reverted ÷ total edits

**Outcome — did it actually land**
- ◐ **resolved** — completed to the user's satisfaction vs abandoned/handed-off
- ◐ **first-try success** — first proposal stuck (no correction)
- ◐ **verified-before-done** — did Claude verify vs just claim "done"
- ✓ **escalations** — pasted_errors + interruptions as a proxy for "user had to step in"

**Difficulty — to normalise the above (a hard task earns more rounds)**
- ◐ **difficulty (1–5)** — judged from breadth + ambiguity + novelty
- ✓ **breadth** — files_touched, tool_calls, distinct skills
- ◐ **ambiguity** — clarifying questions asked

**Flywheel attribution — did the system cause the change**
- ✓ **injections** — which lessons fired, when, matched terms (`injections.jsonl`)
- ◐ **injection_used** — was an injected strategy actually followed
- ◐ **mistake_class** — which rubric-class(es) of mistake occurred (ties to lessons)
- ✓ **coverage** — share of sessions whose problem already had a lesson

## The top KPIs (the ones that show improvement)

Chosen because each is a *comparison*, not a count, and most are computable from
structural data alone.

### 1. Per-lesson friction, difference-in-differences — the headline
For a lesson, find sessions whose **topic fingerprint** (`terms`) overlaps its
keywords by ≥3 distinct terms — i.e. sessions that *hit that problem*. Split by
whether the session ran **before** the lesson was activated or **after**. A
naïve `Δ = after − before` here is **a trap** (see the confound below), so we
don't stop there. We compute the *same* before/after gap on **uncovered**
sessions (the ones no lesson matched) — that gap is the global trend — and
subtract it:

```
Δ_matched  = median friction (matched, after)  − median friction (matched, before, trigger excluded)
Δ_baseline = median friction (uncovered, after) − median friction (uncovered, before)
DiD        = Δ_matched − Δ_baseline        ← the reported "net effect"
```

**Negative DiD = the lesson made its problem cheaper beyond whatever moved the
baseline.** Fully structural — no LLM. It's a quasi-experiment, not proof, but
it's the closest the data allows and it does not lie about inert lessons.

### 2. Repeat-mistake rate over time — the north star
Frequency of each **mistake_class** per session, per week. If the flywheel
works, a class's rate **falls after its lesson is created**. (Needs the miner's
`mistake_class` tag; until then, `pasted_errors`/`tool_errors` per session is a
structural stand-in trend.)

### 3. "Mistake avoided" rate
Of sessions matching a lesson *after* it existed, the fraction where that
mistake **did not occur**. Rising = the lesson is pre-empting the error.

### 4. Injection helpful-rate
Injected lessons that were **followed and helped** ÷ injected. Distinguishes
"fired" from "actually useful" (the `helpful`/`harmful` counters).

### 5. Friction trend on covered vs uncovered tasks
Overall friction over time, split by whether a lesson covered the task. If
covered-task friction drifts below uncovered-task friction, the memory is
paying off across the board.

## Measurement design — and its honest limits

**The confound that makes a naïve before/after lie (why DiD exists).** A lesson
is *born from a bad session*. So the "before" window is selected to contain the
worst incident on that topic — and the very act of picking the worst point means
the next sample is almost certainly better **whether or not the lesson did
anything** (regression to the mean). Compound that with the fact that the model
and your own fluency keep improving over calendar time, and a plain
`after − before` will show "improvement" for a lesson that is completely inert.
That is not a measurement; it's a mirror. Three guards, all implemented:

- **Difference-in-differences.** Subtract the same-period before/after change on
  *uncovered* sessions. Model upgrades and operator learning move covered and
  uncovered tasks alike, so they cancel; only a lesson-specific effect survives.
  An inert lesson ⇒ DiD ≈ 0 ⇒ **no verdict** (unit-tested in
  `scripts/test_kpis.py`).
- **Immutable activation split.** The before/after boundary is the lesson's
  `created` timestamp (frontmatter), *not* file mtime — editing or bumping a
  lesson must not march the boundary. Lessons with no `created` fall back to
  their first injection time; with neither, they're excluded from the causal KPI
  rather than guessed at.
- **Trigger exclusion.** The single highest-friction "before" session — almost
  always the incident that spawned the lesson — is dropped, which blunts the
  regression-to-the-mean inflation directly.

Remaining honest limits:

- **Not randomised.** You can't withhold a lesson from yourself, so this is
  *directional evidence*, not proof.
- **N is small early, and effect-gated.** A verdict renders only when *all four*
  cells (matched/uncovered × before/after) clear 5 sessions **and** |DiD| ≥ 1.0.
  Otherwise the dashboard says **"collecting data — can't tell yet,"** which for
  a fresh install is the honest and expected state.
- **Topic matching is lexical.** A session "hits the problem" on ≥3 shared
  terms; too-generic keywords will mis-match. Keep keywords specific.
- **The one-line claim to aim for:** "on tasks that hit a known problem, friction
  fell X *beyond* the trend on unrelated tasks after the lesson (n=a→b)" — never
  a bare before/after number.

## Pipeline

1. `scripts/metrics.py extract <transcript>` → structural record (rounds,
   friction, terms, …).  `metrics.py backfill` scans history into
   `state/session-metrics.jsonl`.
2. The miner (`/flywheel:learn`) runs backfill and adds judged fields
   (difficulty, resolved, mistake_class, injection_used) for the sessions it reads.
3. The dashboard aggregates into the KPIs above — live (`--serve`) or static.
