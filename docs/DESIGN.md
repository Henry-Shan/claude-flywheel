# The Learning Flywheel — a self-improving Claude system for Labshare

**Problem statement (Henry's words, sharpened):** every session's mistakes evaporate. There is
no system that (1) records where Claude went wrong — didn't check the database first, made a
false assumption, picked the wrong dependency — (2) turns those into skills/rules, (3) lets the
next session **dynamically select** the right lesson at the right moment, and (4) holds a
cross-component **memory of how frontend ↔ backend ↔ database relate** that Claude can query
instead of re-deriving (or worse, guessing).

**Design stance:** extend what exists; build only the missing 40%. Audit of current infra:

| Layer | Status today |
|---|---|
| Capture (transcripts) | ✅ **Done, free.** 71 sessions / 342MB JSONL in `~/.claude/projects/-Users-haotianshan-labshare/` |
| Memory store | ✅ 24 curated files + `MEMORY.md` index, auto-recalled at SessionStart |
| Semantic retrieval | ✅ `labshare-memory` MCP — hybrid BM25+dense, 1,568 vectors, callable mid-session |
| Skills + dynamic selection | ✅ Skill descriptions ARE a dynamic selector (Claude matches them per-turn) |
| Architecture docs | ✅ CLAUDE.md (rules) · ARCHITECTURE_NOTES.md (map) · CONCEPTS.md (mental model) |
| **Mistake mining** | ❌ Nobody reads the transcripts. The raw material rots. |
| **Lesson records** | ❌ Memories are *facts*; there is no "mistake → root cause → correction" schema with occurrence counts |
| **Promotion ladder** | ❌ No rule deciding memory vs skill vs CLAUDE.md line; curation is vibes |
| **Dynamic injection** | ❌ Hooks are EMPTY. Recall is SessionStart-only; nothing matches the *current prompt* to past lessons |
| **Component graph** | ❌ Cross-layer knowledge is prose; not machine-checkable, silently rots |

---

## The loop (one picture)

```
              ┌──────────────────────────────────────────────────────────┐
              ▼                                                          │
 work session ──▶ transcript (JSONL, automatic)                          │
              │                                                          │
              ├─ SessionEnd hook ──▶ outcome log (1 line/session)        │
              │                                                          │
        L1 MINER (/learn) — separate session/cron reads transcripts,     │
        critiques against a rubric ──▶ lessons/*.md (structured)         │
              │                                                          │
        L2 PROMOTION (weekly /consolidate, human-gated)                  │
              ├─ fact            → memory file (existing dir)            │
              ├─ procedure       → skill (sharp trigger description)     │
              ├─ universal rule  → CLAUDE.md line (budgeted!)            │
              └─ architecture    → slice registry (L4)                   │
              │                                                          │
        L3 RETRIEVAL feeds the NEXT session ──────────────────────────────┘
              ├─ SessionStart memory recall            (exists)
              ├─ skill trigger descriptions            (exists — sharpen)
              ├─ labshare-memory semantic search       (exists — make habitual)
              └─ UserPromptSubmit hook: symptom→lesson injection  (NEW — the truly dynamic piece)
```

---

## L1 — The Miner: turn transcripts into lessons

**What:** a `/learn` skill (plus an optional weekly cron agent) that reads one or more session
transcripts and critiques them against a fixed rubric. It runs in a **separate session** — the
working session must not grade itself (biased, and it burns working context).

**The rubric** (each finding must cite the transcript turn as evidence):

| Pattern | Signal in transcript | Example from this repo's history |
|---|---|---|
| **Unverified assumption** | Claim made, later contradicted by a check | Assumed lab-scoped `GetMachineByID` covers dept machines → PR #348 cancel 500 |
| **Skipped ground truth** | Wrote a query/migration without reading schema first | `labs.deleted_at` filter shipped before checking the column existed → every lab read 500'd |
| **Wrong-path choice** | Approach/dependency reversed later in session or by user | Direct browser fetch to frankfurter.app → CORS → had to proxy through /api |
| **Rework loop** | User corrected the same thing ≥2 times | "not lab view, department view" style re-dos |
| **Missing context** | User had to explain something durable mid-task | "grants are always USD" |
| **Win** | A move that resolved fast and is repeatable | Probing the live port to distinguish stale-server 404 from missing route |

**Lesson record** — one file per lesson in `.claude/lessons/`, structured so it's both
greppable and promotable:

```markdown
---
id: check-schema-before-query
class: skipped-ground-truth        # rubric class
symptom: "500s on reads after adding a DB filter/column reference"
signal: user-correction            # ground truth: user-correction | ci-failure | reverted-pr | test-fail | self-judged (weakest)
occurrences: 2                      # bumped by the miner on recurrence
helpful: 3                          # ACE-style counters — bumped when an injected/retrieved
harmful: 0                          #   lesson helped vs misled; drives retention & ranking
sessions: [83c7c99b/t412, 41ab.../t88]   # evidence pointers
tier: memory                        # memory | skill | claude-md | slice  (miner recommends, human ratifies)
status: active                      # active | promoted | retired
---
**Strategy (the generalized, transferable rule — ReasoningBank-style; this is what gets
retrieved/injected):** treat the LIVE data store as ground truth, never repo artifacts.
Before writing any query/filter against Supabase or Mongo, verify the field exists in the
running environment; a migration file in the repo proves nothing about any environment.

**Incident (evidence, kept for audit):** referenced `labs.deleted_at` in queries before the
column existed → every lab read 500'd. Root cause: trusted the migration *file*.
**How to check:** `curl .../rest/v1/labs?select=<col>&limit=1` → 42703 = missing.
```

Two schema rules that matter (from ReasoningBank's ablations — memory *quality* beats
quantity): the **strategy** must be abstracted above the incident (an incident log only helps
on near-identical repeats; a strategy transfers), and the **signal** field records *why we
believe* the label — prefer real outcome signals (user corrections, CI, reverted PRs, which
this environment emits for free) over self-judgment, which is the noisiest labeler.

**Contrastive mining (MaTTS, adapted):** whenever parallel trajectories exist for the same
problem — ship-task's blind test-agent vs impl-agent, `/manager` batch dispatches, Workflow
fan-outs, or a deliberate "try 3 approaches" on a stubborn bug — the miner compares them and
distills the *difference* ("A probed the live port and found the stale server; B assumed the
route was missing"). Contrast between success and failure on the SAME task is the highest-
quality distillation signal available, and this repo's multi-agent infra produces it for free.

**Invocations:** `/learn` (mine the last session) · `/learn <session-id>` (a specific one) ·
`/learn --since 7d` (cron mode). The miner also does **recurrence matching**: before writing a
new lesson it searches existing ones (grep + `labshare-memory` search) and bumps
`occurrences` instead of duplicating — recurrence is the promotion signal.

---

## L2 — Promotion: the ladder that decides *where* a lesson lives

The core taxonomy — this is what makes the system coherent instead of a junk drawer:

| Tier | What belongs | Cost of a bad entry | Gate |
|---|---|---|---|
| **Lesson** (`.claude/lessons/`) | Every mined finding. Cheap, searchable, not in context. | ~zero | automatic |
| **Memory** (existing dir) | Durable *facts* Claude should recall: env quirks, infra topology, prefs | low (recall is selective) | miner writes, human can prune |
| **Skill** | *Procedures* — multi-step how-tos with a trigger: "debugging api.labshare.app 522s", "EAS iOS build failures" | medium (bad trigger = noise) | human ratifies at weekly review |
| **CLAUDE.md line** | *Universal behavioral rules* only: "verify schema against the live store before writing queries" | **high** — loaded into EVERY session; bad rules poison everything | **human-only**, hard budget (~10 lines max for learned rules) |
| **Slice registry** (L4) | Architecture edges: route ↔ handler ↔ collection | low (CI-verified) | automatic + CI check |

**Promotion rules:** occurrence 1 → stays a lesson (searchable). Occurrence ≥2 **or**
procedural → promote to memory/skill — with a **Voyager-style admission bar**: a lesson
becomes a skill only after ≥2 *evidenced* uses (it demonstrably worked when followed), not on
first sighting. Class-level pattern across many lessons ("skipped ground truth" keeps
happening) → one CLAUDE.md rule, not N entries. A weekly `/consolidate` pass (can extend the
existing `/retro`) dedupes, merges, retires stale lessons, and presents promotion candidates
for a 5-minute human yes/no. **Automation at capture and mining; a human at the promotion
gate** — that's the honest division, because auto-promoting into every session's context is
how you poison the well.

**Edit discipline (ACE — avoid "context collapse"):** evolving artifacts (CLAUDE.md, skills,
lessons) are NEVER wholesale-rewritten by an agent — full rewrites lose hard-won detail a
little more each cycle. All changes are **itemized delta operations** — `ADD` / `UPDATE` /
`DELETE` / `NOOP` per entry (the Mem-α operation set, driven heuristically by the metrics
instead of RL) — with the `helpful`/`harmful` counters deciding retention and rank. Corollary
from the same work: *detail is good* in skills and lessons (comprehensive playbooks beat
concise ones — see `/eas`, this repo's own proof); the brevity budget applies only to the
always-on tier (CLAUDE.md), because that's the only tier paying context cost in every session.

**Skill revision, not just creation (Memp):** sustained gains come from *updating* procedures
as evidence accrues — `/consolidate`'s job includes folding new incidents into existing skills
(the `/eas` skill accreted its fix-list exactly this way; formalize that pattern) and retiring
steps that stopped being true.

---

## L3 — Dynamic selection: how lessons reach a live session

Four channels, cheapest-first. "Dynamic" is not magic — it's *matching the current moment to
stored experience*, and each channel matches at a different moment:

1. **Skill trigger descriptions** (exists — sharpen the convention). Claude Code matches skills
   by description every turn, so the description IS the dynamic selector. Convention for
   learned skills: **symptom-first** — "Use when: 522/CORS-looking errors on api.labshare.app…"
   not "AWS operations helper." A skill nobody triggers is a dead lesson.

2. **SessionStart memory recall** (exists). Facts with good one-line hooks in `MEMORY.md`
   surface on their own.

3. **Mid-session semantic search** (exists — make it habitual). The `labshare-memory` MCP
   (hybrid BM25+dense) is callable *during* work. Add ONE rule to CLAUDE.md:
   > Before debugging an error or designing against the DB/API, search lessons+memory for the
   > symptom first (`labshare-memory` search + grep `.claude/lessons/`).
   This converts the index from "nice to have" into the first move of every investigation. Index
   `.claude/lessons/` into the same vector store so one search covers both.

4. **`UserPromptSubmit` hook — the NEW, truly dynamic piece.** Hooks are currently empty; this
   is the highest-leverage addition. A small script receives each user prompt, does a fast match
   (keyword + optional embedding) against lesson `symptom:` lines, and **injects the top 1–2
   matching corrections into context** before Claude responds:
   ```
   user: "clicking delete lab gives a 500"
   hook: [lesson check-schema-before-query]: 500s after adding a DB filter — verify the
         column exists against the LIVE store first (42703 = missing). Also: Go doesn't
         hot-reload; confirm the running server has your code (probe the port).
   ```
   Now the lesson arrives at the *exact moment* of relevance, with zero human effort and zero
   standing context cost. Budget: ≤2 injections, ≤4 lines each, only above a similarity
   threshold — silence is better than noise. What gets injected is the **strategy** line, not
   the incident. Ranking is Generative-Agents-style, not pure similarity:
   `score = similarity × recency-decay × importance`, where importance ≈
   `occurrences + helpful − 2·harmful` — lessons that keep recurring and keep helping rise;
   lessons that never fire or mislead decay toward retirement (the "forgetting" half of the
   memory lifecycle that most systems skip).

---

## L4 — Component-graph memory: the cross-layer understanding

**Problem:** "which Go handler serves this button, which collection does it write, is it
lab-scoped?" is re-derived (or hallucinated) every session. ARCHITECTURE_NOTES/CONCEPTS teach
the *pattern*, but per-feature edges are prose that silently rots.

**Design: a slice registry** — `.claude/slices/*.yaml`, one file per vertical slice, machine-
checkable:

```yaml
# .claude/slices/inventory.yaml
slice: inventory
ui:
  mobile: apps/mobile/app/(app)/(inventory)/    # add-item.tsx, inventory.tsx …
  web:    apps/web/components/inventory/InventoryView.tsx
hook:     packages/shared-react/src/hooks/inventory.ts          # mobile only
endpoint: packages/shared-core/src/api/endpoints/inventory/
routes:
  - { http: "POST /insert-data", handler: insertData, file: apps/server/repository/inventory_handlers.go }
  - { http: "POST /search",      handler: search,     file: apps/server/repository/inventory_handlers.go }
storage:  { db: mongo, ref: "Items.Items", tenancy: labId }
notes:
  - "web does NOT use React Query — direct shared-core calls"
  - "insert force-stamps item.LabID from the token; reads use GetLabFilter"
```

Three properties make this better than prose:
- **Queryable**: "grep the slice files" answers cross-layer questions in one step; the
  UserPromptSubmit hook can inject the relevant slice when a prompt mentions its feature.
- **CI-verified so it can't rot**: a tiny script asserts every referenced path exists and every
  `http:` route appears in a `router.Handle` line. Stale architecture docs are *worse* than
  none — this is the fix.
- **Feeds everything else**: the miner links lessons to slices; `/graphify` can build its richer
  graph (communities, god nodes) from the same registry when deeper analysis is wanted.

Seed the first 6–8 slices (inventory, orders, grants, tools, reservations, messaging,
invoices, auth) by distilling ARCHITECTURE_NOTES.md — a one-time hour.

---

## Research grounding — what we adopted, from where, and what we rejected

The design is deliberately positioned in the **memory-based self-improvement** branch of the
literature (the only branch available: Claude is a fixed model behind an API — no weight
updates, no RL gradients). Mapping of the relevant work onto this scenario:

| Work | Adopted here | Where |
|---|---|---|
| **ReasoningBank** (2509.25140) | The backbone loop: distill *strategy-level* memory from success AND failure experience; retrieve at task time; integrate learnings back per-task | L1 schema (`Strategy` vs `Incident`), per-session SessionEnd mining |
| **MaTTS** (ReasoningBank's scaling) | Contrastive mining over parallel trajectories — this repo's ship-task/`/manager`/Workflow fleet already *produces* parallel attempts; we mine the contrast instead of paying for new rollouts | L1 "Contrastive mining" |
| **ACE** (agentic context engineering) | Context artifacts evolve by **itemized deltas, never wholesale rewrites** (context collapse); helpful/harmful counters; detail-is-good except in the always-on tier | L2 "Edit discipline" |
| **Memento** (case-based, no fine-tuning) | Validates the architecture: lessons = case memory, `labshare-memory` hybrid index = the non-parametric retriever. Nothing extra to build | L3 |
| **Memp / AWM / SkillWeaver / Voyager** | Skills as a verified procedural library: two granularities (runbooks + heuristics), admission only after evidenced use, and **revision of existing skills** as the main source of sustained gains | L2 promotion + skill-revision rules |
| **Reflexion** | The miner itself — verbal reflections from feedback; upgraded by preferring *real* outcome signals over self-judgment | L1 `signal` field |
| **Generative Agents** | Retrieval scoring = similarity × recency × importance; explicit forgetting/decay | L3 ranking |
| **MemGPT** | Names what's already true here: context = working memory, memory dir + lessons + MCP search = archival, hooks/search = paging | — |
| **Mem-α / Memory-R1** | ❌ RL-learned memory ops don't transfer (no gradients). Kept only the ADD/UPDATE/DELETE/NOOP operation taxonomy, driven heuristically by the metrics | L2 |
| **Darwin Gödel Machine** | ❌ Open-ended self-modification rejected (blast radius). Kept the kernel: meta-artifacts (miner prompt, thresholds, consolidation policy) are versioned in git and revised by the weekly loop itself — the archive is git history | below |

**Two scenario-specific advantages over the papers:**
1. **Ground-truth labels for free.** ReasoningBank must self-judge its trajectories (noisy).
   This environment emits real outcome signals — user-correction turns, CI failures, reverted
   PRs, `/feature-tester` verdicts. The miner prefers them always; the `signal` field records
   which labeler produced each lesson so weak (self-judged) lessons rank lower.
2. **MaTTS is nearly free.** The paper spends extra compute generating diverse rollouts for
   contrastive signal; the ship-task blind-test/implement split and `/manager` batches already
   generate parallel trajectories on the same problems. Mining their contrast is a prompt
   change, not a compute budget.

**The meta-loop (SelfMem principle, DGM-lite mechanism):** the memory *pipeline itself* is a
target of improvement. The miner prompt, injection thresholds, and consolidation policy are
files in git; the weekly `/consolidate` reviews the metrics (below) and may propose diffs to
those files — human-gated like every promotion. Self-optimizing, but with git history as the
archive and a human at the gate rather than open-ended self-modification.

**Honest expectations:** these techniques deliver real but *incremental* gains in the papers
(single-digit-to-low-double-digit percent on agent benchmarks). The practical dominators are
retrieval precision and label quality — a noisy lesson injected at the wrong moment is
negative value. Hence the admission bars, counters, decay, and silence-over-noise thresholds
throughout. If repeat-mistake rate doesn't fall, the loop is decorative — kill or fix it.

## Packaging — a separate repo, plug-and-play via a Claude Code plugin

The system lives in its **own repo** and installs into any project as a **Claude Code plugin**
(the same mechanism as `superpowers`/`vercel` in this setup: a git repo with a
`.claude-plugin/marketplace.json`; plugins ship skills + hooks + commands, and plugin hooks
auto-activate on install).

```
claude-flywheel/                      ← the system (CODE only, generic)
├── .claude-plugin/{plugin.json, marketplace.json}
├── skills/{learn, consolidate, flywheel-init}/SKILL.md
├── hooks/{hooks.json, inject.py, outcome_log.py}   ← stdlib-only, zero deps
└── schema/lesson.md

<project>/.claude/                    ← per-project DATA (git-versioned with the project)
├── lessons/  ·  slices/  ·  flywheel.json

~/.claude/flywheel/lessons/           ← GLOBAL tier (cross-project strategies)
```

**Three-way split that makes it portable:**
- **Code** (miner, hooks, consolidator) — plugin repo; update once, every project benefits.
- **Data** (lessons, slices, metrics) — the consuming project's `.claude/`, so lessons ride
  the project's git (review in diffs, `git pull` on any machine/CI runner inherits them).
- **Scope** — every lesson is tagged `scope: project | global`. Process strategies ("a
  swallowed catch on a gating fetch is the prime suspect") are global and shared across all
  projects via `~/.claude/flywheel/`; domain facts ("dept machines have no labId") stay
  project-tier. The injection hook and `/learn`'s recurrence matching search both tiers.

**Per-project onboarding = 2 commands:** `/plugin install flywheel` (once per machine) →
`/flywheel-init` inside the project (creates `.claude/lessons/` + config, offers the one
CLAUDE.md habit line, optionally seeds slices from existing docs). Hooks are live immediately.

## Worked example — mining a real session (the lab-selector bug)

A real session: "PR #515's lab selector isn't showing." The account had 8 valid labs; the code
provably rendered the control; the user repeatedly reported "still cannot see." Claude explored
five hypotheses (stale bundle → memoization → early returns → prop wiring → remount race)
across several user reload-and-paste rounds before triangulated logs exposed the truth: a
**flaky `getMyLabs` whose `Network Error` was swallowed by `.catch(() => {})`**, leaving the
gating state empty forever. Fix: bounded retry + cancel guard.

What the miner extracts (both are seeded in `.claude/lessons/` as the first real entries):

1. **`silent-catch-hides-gated-ui`** (class: missed-prime-suspect, scope: global) — the
   *contrastive* lesson. The empty catch was visible in the first file read and even remarked
   on, yet five render-tree theories were tried first. Strategy: *intermittent* symptom ⇒
   nondeterministic cause class (network/timing/race), and a swallowed rejection on a gating
   fetch is the prime suspect — grep for it before any render-tree theory. Had this lesson been
   injected when the user typed "the control isn't showing (sometimes)", the session shortens
   from ~6 rounds to ~2.
2. **`instrument-every-hop-in-one-pass`** (class: win, tier: skill-candidate, scope: global) —
   the move that cracked the case, minus its inefficiency: instrument fetch + parent render +
   child gate in ONE edit under one tag, so a single human reload/paste triangulates the losing
   layer. Key resource insight: in interactive debugging, *user round-trips* are the scarcest
   resource. Corollary: an instrumented component whose log never appears = stale bundle first,
   not "component doesn't render." Procedural ⇒ on recurrence it graduates to a
   "missing-UI diagnosis" runbook skill.

This is the system working as intended: the session's failed paths and successful path exist
side-by-side in the transcript; the miner distills the *decision rule that would have
shortcut the search*, tags it with a real signal (user corrections + confirmed fix), scopes it
globally, and the injection hook meets the next "I can't see X" prompt with it — in *any*
project the plugin is installed in.

## Metrics — is it actually learning?

The `/consolidate` weekly report tracks, per week:
- **Repeat-mistake rate**: lessons whose `occurrences` bumped (the number that should fall)
- **Corrections-per-session**: user-correction turns per session (miner counts them)
- **Injection hit-rate**: how often a hook-injected lesson was used vs ignored
- **Skill fire-rate**: learned skills that triggered (dead skills get their descriptions fixed
  or get retired)

If repeat-mistake rate doesn't fall after ~a month, the loop is decorative — kill or fix it.

## What NOT to build (deliberate scope cuts)

- **No fine-tuning / model training.** All learning is context engineering; that's what's
  actually available and it's auditable.
- **No new vector database.** The `labshare-memory` hybrid index exists; extend it to lessons.
- **No fully-automatic CLAUDE.md writes.** Highest blast radius, human-only.
- **No always-on giant context.** The whole design is *selective* recall: lessons live outside
  context and arrive only when matched. Stuffing everything in would be simpler and worse.
- **No self-grading mid-session.** Mining is a separate, cold read of the transcript.

## Build order (each phase useful alone)

| Phase | Build | Effort | Payoff |
|---|---|---|---|
| **1** | Scaffold the **plugin repo** (`claude-flywheel`): lesson schema (v2: Strategy/Incident split, `signal`, `scope`, counters) + `/learn` miner skill + `/flywheel-init`; install into labshare; the one "search before debugging" CLAUDE.md rule. (Two seed lessons already exist in `.claude/lessons/`.) | ~half day | Mistakes stop evaporating; portable from day one |
| **2** | `/consolidate` (extend `/retro`): delta-op consolidation, promotion gate, metrics; SessionEnd hook = outcome log **+ auto-trigger `/learn`** (per-session loop, ReasoningBank-style) | ~half day | The ladder closes per-session, not just weekly |
| **3** | Slice registry (seed 6–8) + CI path-checker | ~half day | Cross-component queries grounded, rot-proof |
| **4** | `UserPromptSubmit` injection hook (keyword first, embeddings later); index lessons into labshare-memory; scored ranking + decay | ~1 day | True dynamic recall at the moment of need |
| **5** | Contrastive mining: teach `/learn` to pair parallel trajectories (ship-task test/impl agents, `/manager` batches, Workflow fan-outs) and distill the success/failure *difference* | ~half day | The highest-quality lessons, from compute already spent |

---

*The one-sentence version: transcripts are already the experience; this system adds a miner to
extract lessons, a ladder to put each lesson where it belongs (lesson → memory → skill →
rule → slice), and three retrieval channels — sharpened skill triggers, habitual semantic
search, and a prompt-time injection hook — so the next session meets the mistake **before**
making it.*
