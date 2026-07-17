---
name: learn
description: >
  Mine Claude Code session transcripts into durable, strategy-level lessons.
  Use when: the user says "/learn", "learn from this", "that was wrong —
  remember it", "why did you keep getting that wrong", after a debugging
  session that involved user corrections or a reverted approach, or to process
  the pending mining queue. Arguments: no args = mine the current/most recent
  session; "--queued" = process the pending-mine queue; "--since 7d" = mine the
  last N days of sessions; a session id or transcript path = mine that one.
argument-hint: "[--queued | --since 7d | <session-id-or-transcript-path>]"
---

# /flywheel:learn — the miner

You are the mining stage of the claude-flywheel learning loop. Your job: read
session transcript(s), critique them against a fixed rubric, and distill
**strategy-level lessons** into the lesson store — so future sessions meet a
known mistake *before* re-making it.

**Prime directives**
1. **Quality over quantity.** ≤3 lessons per session. A lesson must encode a
   *transferable decision rule*, not an incident log. Skip trivia.
2. **Prefer real outcome signals over self-judgment.** A lesson backed by a
   user correction, CI failure, reverted PR, or failing test is gold; your own
   assessment of "that looked wrong" is the weakest signal (`self-judged`).
3. **Recurrence beats novelty.** Before writing a new lesson, search existing
   ones; if the same root cause exists, bump `occurrences` and append evidence
   instead of creating a duplicate.
4. **Never touch CLAUDE.md.** Promotion to always-on rules is
   `/flywheel:consolidate`'s job, human-gated.

## Step 1 — Resolve which transcript(s) to mine

Transcripts live at `~/.claude/projects/<munged-cwd>/*.jsonl`, where
`<munged-cwd>` is the project path with `/` replaced by `-` (e.g.
`/Users/foo/bar` → `-Users-foo-bar`). Each file is one session (JSONL of
messages). Resolve by argument:

- **No args:** the most recently modified transcript for the current project
  (`ls -t ~/.claude/projects/<munged-cwd>/*.jsonl | head -2` — the top file is
  usually THIS session; prefer the most recent *completed* one, or mine this
  session's earlier portion).
- **`--queued`:** read `~/.claude/flywheel/state/pending-mine.jsonl`; mine each
  entry with `"mined": false`, oldest first, then rewrite those entries with
  `"mined": true`.
- **`--since <N>d`:** transcripts with mtime in the window, for this project.
- **Explicit arg:** treat as a path if it exists, else glob
  `~/.claude/projects/*/<arg>*.jsonl`.

## Step 2 — Read the transcript EFFICIENTLY (they can be tens of MB)

Do NOT read a transcript end-to-end. Target the signal:

```bash
wc -c <transcript>                     # size check first
# Find correction/failure hotspots (line numbers):
grep -n -i -E '"type":"user"' <t> | wc -l          # session shape
grep -n -i -E "(actually|that's wrong|not what|still (can't|cannot|doesn)|you broke|revert|undo|instead of|no[,.] )" <t> | head -40
grep -n -E '"is_error":true|error|failed|FAIL' <t> | head -40
```

Then read windows (~20–60 lines) around hotspots with `sed -n 'START,ENDp'`.
Reconstruct each incident: what was claimed/attempted → what contradicted it →
what finally worked. User messages immediately after an assistant action are
the highest-value lines: they contain the correction.

## Step 3 — Critique against the rubric

| class | signature in transcript |
|---|---|
| `unverified-assumption` | a claim later contradicted by a check ("assumed X covers Y") |
| `skipped-ground-truth` | wrote code against a DB/API/env without checking the live thing first |
| `wrong-path` | an approach or dependency chosen, then reversed later |
| `missed-prime-suspect` | the true cause was visible/mentioned early but pursued last |
| `rework-loop` | the user corrected the same thing ≥2 times |
| `missing-context` | the user had to explain something durable mid-task |
| `win` | a move that cracked the problem fast and is repeatable |

**Contrastive mining (highest-quality signal):** if the transcript contains
parallel attempts at the same problem — multiple subagents, a test-agent vs an
implement-agent, retried approaches, or sibling sessions on the same bug —
diff the successful path against the failed ones and distill the *decision
rule that separates them*. That contrast IS the lesson.

## Step 4 — Recurrence check (before writing anything)

For each candidate lesson, search BOTH tiers:

```bash
grep -ril "<distinctive terms>" <project>/.claude/lessons/ ~/.claude/flywheel/lessons/ 2>/dev/null
```

Also search any project memory index if present. If an existing lesson shares
the root cause: bump `occurrences`, append the new session to `sessions:`,
enrich `keywords:` with any new trigger phrasing, and (if the new incident
adds a nuance) extend the Strategy. Do NOT create a near-duplicate.

## Step 5 — Write the lesson(s)

Decide the tier directory by **scope**:
- `scope: global` → `~/.claude/flywheel/lessons/` — the test: *would this
  exact advice help in an unrelated codebase?* (process strategies, debugging
  heuristics, tool-usage rules)
- `scope: project` → `<project>/.claude/lessons/` — domain facts, this repo's
  quirks, environment topology.

File name = `<id>.md`. Use exactly this schema:

```markdown
---
id: <kebab-case-slug>
class: <rubric class>
scope: global | project
symptom: "<how this problem announces itself, in the words a user would type>"
keywords: "<8–15 comma-separated trigger terms/synonyms — include the words a
  frustrated user would actually use: 'not showing', 'flaky', 'sometimes',
  'still broken'. These drive automatic injection; be generous with synonyms,
  but avoid terms so generic they'd fire on unrelated work>"
signal: user-correction | ci-failure | reverted-pr | test-fail | self-judged
occurrences: 1
helpful: 0
harmful: 0
sessions: [<session-id or transcript basename>/<approx location>]
tier: lesson            # lesson | skill-candidate — /consolidate promotes
status: active
---
**Strategy (retrieved/injected):** <the GENERALIZED, transferable decision
rule. Written as advice to a future agent facing the symptom. 3–10 lines.
This block is what gets auto-injected — make it self-contained.>

**Incident (evidence):** <what actually happened, 2–6 lines: the wrong move,
the cost (how many rounds/how much rework), the fix. Kept for audit; not
injected.>
```

Schema rules that matter:
- The **Strategy must be abstracted above the incident** — an incident log
  only helps on identical repeats; a strategy transfers.
- **keywords are the retrieval surface.** The injection hook is lexical
  (stdlib, no embeddings) — rich synonyms are what make dynamic recall work.
- **helpful/harmful counters:** if the transcript shows a previously-injected
  lesson was followed and helped, bump its `helpful`; if one misled, bump
  `harmful` (check `~/.claude/flywheel/state/injections.jsonl` for what was
  injected into the mined session).

## Step 6 — Report

End with a compact summary: lessons written (id, class, scope, signal),
lessons bumped (id, occurrences), lessons skipped as trivia, and — if any
lesson looks procedural and recurring — flag it as a `/flywheel:consolidate`
promotion candidate. If mining `--queued`, mark processed queue entries
`"mined": true`.
