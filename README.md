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
| `/flywheel:learn` | mine the latest session ("that was wrong — remember it") |
| `/flywheel:learn --queued` | process everything the SessionEnd hook queued |
| `/flywheel:learn --since 7d` | mine the last week of sessions |
| `/flywheel:consolidate` | weekly: dedupe, merge, promote, retire + metrics report |

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
    "enabled": true,      // kill-switch for the prompt-time injection
    "maxInjections": 2,   // max lessons injected per prompt
    "minScore": 6,        // weighted keyword-match threshold
    "minDistinct": 2      // distinct matched terms required
  }
}
```

State (injection audit log, session outcomes, mining queue) lives in
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

## Uninstall

`/plugin uninstall flywheel@claude-flywheel` (or remove the settings entries).
Your lessons are plain markdown in your repos — they remain yours.

## License

MIT
