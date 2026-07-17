# claude-flywheel

**Self-improving memory for Claude Code.** Every session's mistakes currently
evaporate: the wrong assumption, the dependency that had to be reversed, the
bug that took six round-trips because the prime suspect was checked last.
Flywheel closes the loop:

```
 work session ─▶ transcript (automatic)
      │
      ├─ SessionEnd hook ─▶ outcome log + mining queue
      │
 /flywheel:learn ─ mines transcripts against a failure/win rubric
      │            └─▶ .claude/lessons/*.md   (strategy-level, evidence-backed)
      │
 /flywheel:consolidate ─ weekly: dedupe · promote (lesson → skill → rule) · metrics
      │
 next session:
      ├─ UserPromptSubmit hook matches your prompt to lesson symptoms and
      │  INJECTS the relevant strategy before Claude answers
      └─ so the session meets the mistake BEFORE re-making it
```

No fine-tuning, no external services, no dependencies — pure context
engineering with Python-stdlib hooks. Grounded in the memory-based
self-improvement literature (ReasoningBank's strategy-level distillation and
failure/success mining, ACE's delta-update discipline, Voyager/Memp-style
verified procedural promotion, Generative-Agents retrieval scoring) — see
[docs/DESIGN.md](docs/DESIGN.md).

## Install (once per machine)

In any Claude Code session:

```
/plugin marketplace add Henry-Shan/claude-flywheel
/plugin install flywheel@claude-flywheel
```

Or settings-based (e.g. `~/.claude/settings.json` for all your projects, or a
project's `.claude/settings.json` to offer it to the whole team):

```json
{
  "extraKnownMarketplaces": {
    "claude-flywheel": {
      "source": { "source": "github", "repo": "Henry-Shan/claude-flywheel" }
    }
  },
  "enabledPlugins": { "flywheel@claude-flywheel": true }
}
```

Requires `python3` on PATH (macOS/Linux default; on Windows ensure the
`python3` alias exists).

### Staying current (auto-update)

Claude Code pins a plugin to the version present at install time; third-party
marketplaces default to auto-update **off**, so it's easy to silently run stale
code. Flywheel guards against that two ways:

- **Update notice** — a `SessionStart` hook checks GitHub (at most once/day,
  stdlib, fail-silent) and, if a newer version is published, prints a one-line
  notice with the `/plugin update flywheel@claude-flywheel` command. It never
  touches the plugin cache (that would desync Claude Code's version tracking) —
  it only tells you.
- **True auto-update (recommended)** — enable auto-update for the
  `claude-flywheel` marketplace once, in `/plugin` → Marketplaces. Claude Code
  then pulls new versions on its own (applied on next launch / `/reload-plugins`).

Either way you only ever run one explicit update; after that it keeps itself
current.

## Set up a project (once per project, ~30s)

```
cd your-project
/flywheel:init
```

Creates `.claude/lessons/` + `.claude/flywheel.json`, verifies the hooks fire,
optionally adds a search-before-debugging habit line to CLAUDE.md, and offers
to seed lessons from your existing transcripts.

## Daily use

Mostly: **nothing**. Work normally. Transcripts record automatically; the
SessionEnd hook queues substantial sessions for mining; when you type a prompt
whose symptoms match a stored lesson, the strategy is injected automatically
(max 2, strict threshold, at most once per lesson per session — silence over
noise).

When you want control:

| command | what it does |
|---|---|
| `/flywheel:status` | **is it working?** health check + a visual dashboard (lessons, injection timeline, metrics) |
| `/flywheel:learn` | mine the latest session ("that was wrong — remember it") |
| `/flywheel:learn --queued` | process everything the SessionEnd hook queued |
| `/flywheel:learn --since 7d` | mine the last week of sessions |
| `/flywheel:consolidate` | weekly: dedupe, merge, promote, retire + metrics report |

### The dashboard

Two ways to view it:

- **`/flywheel:serve`** — a **live local web server** (`http://127.0.0.1:8787`)
  that auto-refreshes every 5s. This is the "always on" view.
- **`/flywheel:status`** — writes a static snapshot to
  `~/.claude/flywheel/dashboard.html` (open in any browser), plus a terminal
  health summary.

Both show:

- **Health** — plugin installed? hooks runnable? autopilot on?
- **"Did it make Claude better?"** — the headline, done honestly: for each
  lesson it takes *matched tasks* (sessions that hit its problem), compares
  friction before vs after the lesson was activated, and **subtracts the same
  before/after change on unrelated tasks** (a difference-in-differences). That
  nets out model upgrades and you-getting-faster, so an inert lesson reads as
  *no effect* instead of fake improvement. The split is on an immutable
  `created` stamp, the triggering session is excluded, and a verdict only shows
  once each side clears 5 sessions. Until then it says **"can't tell yet."** See
  [docs/METRICS.md](docs/METRICS.md).
- **Injection feed** — every lesson that fired, when, and which terms matched.
- **Lesson catalog** — scope, class, signal, occ/helpful/harmful, last-fired.

**Metrics stay fresh on their own:** a cheap, LLM-free structural extractor
(`scripts/metrics.py`) runs on every session end (via the SessionEnd hook) and
records rounds, corrections, interruptions, errors, and a composite *friction*
score per session into `state/session-metrics.jsonl`. To seed history now:
`python3 scripts/metrics.py backfill --since 60d`.

A freshly-installed flywheel shows *0 injections* and *"not enough matched
sessions yet"* — both expected; they fill in as you work.

## What a lesson looks like

```markdown
---
id: silent-catch-hides-gated-ui
class: missed-prime-suspect
scope: global
symptom: "UI element intermittently missing though code says it should render"
keywords: "not showing, missing, can't see, hidden, sometimes, intermittent,
  flaky, selector, swallowed error, catch, silent failure, vanishes"
signal: user-correction
occurrences: 1
helpful: 0
harmful: 0
created: 2026-07-16T14:03:00Z
tier: lesson
status: active
---
**Strategy (retrieved/injected):** When a UI element is *intermittently*
missing and its visibility is gated on async-fetched state, check the fetch's
failure path FIRST. An empty `.catch(() => {})` on a gating fetch means one
transient failure permanently hides the element. Intermittent symptom ⇒
nondeterministic cause (network/timing/race), not static logic.

**Incident (evidence):** Lab selector "not showing"; five render-tree
hypotheses explored before logs exposed a flaky fetch whose Network Error was
swallowed. Fix: bounded retry + cancel guard.
```

The **Strategy** is what gets injected. **keywords** drive the matching
(lexical + light stemming — no embeddings, no network). **occurrences /
helpful / harmful** drive ranking, promotion, and eventual retirement.

## The promotion ladder

| tier | what belongs | gate |
|---|---|---|
| lesson (`.claude/lessons/`) | every mined finding | automatic |
| skill | procedures, on ≥2 evidenced uses | human approves at `/flywheel:consolidate` |
| CLAUDE.md line | universal behavioral rules (budget ~10 lines) | human-only |
| global tier (`~/.claude/flywheel/lessons/`) | strategies that hold in ANY codebase | automatic (scope field) |

Project-tier lessons live in the project's git — code-review them like code,
and every machine/CI runner inherits them on pull. Global-tier lessons follow
*you* across projects.

## Configuration (`.claude/flywheel.json`)

```json
{
  "version": 1,
  "injection": {
    "enabled": true,
    "maxInjections": 2,
    "minScore": 6,
    "minDistinct": 2,
    "semanticRerank": false,
    "embedderCmd": ""
  }
}
```

- `enabled` — kill-switch for prompt-time injection in this project
- `maxInjections` — max lessons injected per prompt
- `minScore` — weighted keyword-match threshold (higher = quieter)
- `minDistinct` — distinct matched terms required
- `semanticRerank` — opt-in. When `true` AND a `state/embeddings.json` vector
  cache exists AND `embedderCmd` is set, cosine similarity reranks the
  lexically-matched candidates. Off by default; the matcher is otherwise pure
  stdlib (keyword + light stemming + synonym canonicalization + TF-IDF rarity
  weighting), no network, no embeddings.
- `embedderCmd` — shell command for the opt-in rerank: reads text on stdin,
  prints a JSON float vector on stdout (e.g. a local embedding model). Only used
  when `semanticRerank` is true; any failure falls straight back to lexical rank.

(Keep it valid JSON — no comments. A malformed config falls back to defaults.)
State (injection audit log, mining queue, event log for metrics) lives in
`~/.claude/flywheel/state/` — never in your repo.

## Design principles (the short version)

- **Strategy-level memory, not incident logs** — incidents only help on
  identical repeats; strategies transfer.
- **Real outcome signals over self-judgment** — user corrections, CI failures
  and reverted PRs label lessons; the `signal` field records which.
- **Silence over noise** — a wrongly-injected lesson is negative value; strict
  thresholds, per-session dedupe, decay toward retirement for lessons that
  never help.
- **Delta updates, never wholesale rewrites** — evolving artifacts (lessons,
  skills, CLAUDE.md) change by itemized ops only, with helpful/harmful
  counters; a human gates every promotion into always-on context.
- **If it isn't measured, it's decorative** — `/flywheel:consolidate` reports
  repeat-mistake rate; if that doesn't fall, fix the loop or kill it.

## Autopilot — run the whole loop automatically (opt-in)

By default you run `/flywheel:learn` and `/flywheel:consolidate` yourself.
Turn on **autopilot** and the SessionEnd hook does it for you: after a session
ends it fires a detached, headless `claude -p` run that drains the mining
queue, and on a weekly cadence runs consolidation. Capture → mine → consolidate
→ inject, with no human in the inner loop.

Enable it in `~/.claude/flywheel/config.json`:

```json
{
  "automation": {
    "enabled": true,
    "mineDebounceMinutes": 20,
    "consolidateEveryDays": 7,
    "runTimeoutSeconds": 900,
    "permissionMode": "scoped",
    "model": ""
  }
}
```

- **It spends tokens on its own** — each debounced mining run is a real (small,
  headless) `claude` call. Set `"model"` to a cheaper model to reduce cost.
- **`permissionMode`**: `scoped` (curated allow-list — safest) or `skip`
  (`--dangerously-skip-permissions` — most reliable, bypasses all checks).
- **Safe by construction**: mining sessions carry `FLYWHEEL_AUTOPILOT=1` so
  they can't trigger more mining (recursion guard), a single-flight lock +
  debounce cap runaway spend, and every run is timeout-bounded.
- **Promotions stay human-gated**: auto-consolidation only does reversible
  hygiene + metrics and writes promotion *candidates* to
  `~/.claude/flywheel/state/promotion-candidates.md` for you — it never
  auto-writes a skill or a CLAUDE.md rule.

`/flywheel:status` shows whether autopilot is on and when it last ran.

## Roadmap

- **Slice registry** — a machine-checkable per-feature component map
  (`.claude/slices/*.yaml`) that grounds cross-layer questions; described in
  [docs/DESIGN.md](docs/DESIGN.md) §L4.
- **Embedding-based retrieval** — injection matching is deliberately lexical
  (keywords are the API); an optional embedding index can layer on later
  without changing the lesson format.

## Uninstall

`/plugin uninstall flywheel@claude-flywheel` (or remove the settings entries).
Your lessons are plain markdown in your repos — they remain yours.

## License

MIT
