---
name: learn
description: >
  Mine Claude Code session transcripts into durable, strategy-level lessons.
  Use when: the user says "/flywheel:learn", "learn from this", "that was
  wrong — remember it", "why did you keep getting that wrong", after a
  debugging session that involved user corrections or a reverted approach, or
  to process the pending mining queue. Arguments: no args = mine the most
  recent completed session for this project; "--queued" = process the
  pending-mine queue; "--since 7d" = mine the last N days of sessions; a
  session id or transcript path = mine that one.
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
4. **Classify only after reading the exchange.** Grep hits masquerade: user
   phrasing that *looks* like a correction ("did you see X?") is often just
   navigation. Never write a lesson from an index hit you haven't read.
5. **Never touch CLAUDE.md.** Promotion to always-on rules is
   `/flywheel:consolidate`'s job, human-gated.

## Step 1 — Resolve which transcript(s) to mine

Transcripts live at `~/.claude/projects/<munged-cwd>/*.jsonl`, where
`<munged-cwd>` is the project path with `/` replaced by `-` (e.g.
`/Users/foo/bar` → `-Users-foo-bar`). Each file is one session (JSONL of
messages). Resolve by argument:

- **No args:** the most recent **completed** session for the current project
  (`ls -t ~/.claude/projects/<munged-cwd>/*.jsonl | head -3`). Prefer a
  finished session over the live one — mining is a cold read; grading the
  session you are currently in is biased. Mine the live session's transcript
  only when the user explicitly says "learn from THIS".
- **`--queued`:** read `~/.claude/flywheel/state/pending-mine.jsonl`; mine each
  entry with `"mined": false`, oldest first, then rewrite those entries with
  `"mined": true`.
- **`--since <N>d`:** transcripts with mtime in the window, for this project.
- **Explicit arg:** treat as a path if it exists, else glob
  `~/.claude/projects/*/<arg>*.jsonl`.

## Step 2 — Read the transcript EFFICIENTLY (field-tested recipe)

Real transcripts are large (1–50 MB) and single lines can exceed 100 KB (tool
results, skill listings). **Never** read a transcript end-to-end, and **never**
dump raw line windows with `sed` — project fields instead.

Know the format first:
- Every message is one JSON line. `"type":"user"` lines are BOTH human
  messages AND tool results. The discriminator: a **human message** has
  `type=="user"`, no `toolUseResult` field, and its `.message.content` is a
  plain string or contains `{"type":"text"}` blocks. Tool results carry
  `tool_use_id` items.
- Tool errors are encoded exactly as `"is_error":true` (no space).

Hotspot indexing (in descending signal-to-noise, validated on real data):

```bash
wc -c <t>                                              # size sanity check
grep -n "Request interrupted by user" <t>              # HIGHEST signal: the user stopped you
grep -n '"is_error":true' <t> | head -30               # tool rejections/failures (exact, no space)
grep -n -E "Failed to load resource|Internal Server Error|blocked by CORS|ERROR \[|Traceback" <t> | head -30
                                                       # pasted error text = a real runtime failure the user hit
```

Then extract HUMAN messages only (with line numbers) and scan those — this is
where corrections live:

```bash
jq -rc 'select(.type=="user" and (has("toolUseResult")|not))
        | [input_line_number,
           (.message.content | if type=="string" then . else ([.[]? | select(.type=="text") | .text] | join(" ")) end)]
        | @tsv' <t> | head -80
```

Apply correction-phrase matching ("actually", "that's wrong", "still broken",
"instead", "no,") **only to this extracted human text** — on raw JSONL those
words match injected skill listings and schemas on nearly every line.

For each hotspot, reconstruct the incident by reading the surrounding
*messages* (via the jq projection around those line numbers), not raw lines:
what was claimed/attempted → what contradicted it → what finally worked. The
human message immediately after an assistant action is the highest-value text:
it contains the correction.

## Step 3 — Critique against the rubric

**SCOPE GATE (apply before the rubric): only CODE matters.** Mine only from
material that is about the code itself — a bug being fixed or a feature being
built. IGNORE entirely (do not mine, do not count as corrections, do not let it
shape a lesson): session-management and tool-plumbing chatter ("try again",
"i connected the mcp", "restart", model/permission/login talk), scheduling or
meta requests, pasted announcements/marketing text, and anything else with no
concrete bug or feature in it. A "correction" in a non-code exchange is not a
correction signal. If a whole session contains no code work, skip it and mark
it mined.

| class | signature in transcript |
|---|---|
| `unverified-assumption` | a claim later contradicted by a check ("assumed X covers Y") |
| `skipped-ground-truth` | wrote code against a DB/API/env without checking the live thing first |
| `shipped-untested` | reported "done" on typecheck/build green; the USER was the first runtime tester and hit the failure |
| `wrong-path` | an approach or dependency chosen, then reversed later |
| `missed-prime-suspect` | the true cause was visible/mentioned early but pursued last |
| `rework-loop` | the user corrected the same thing ≥2 times (verify by reading — see prime directive 4) |
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

If the project keeps other memory/architecture notes (e.g. an auto-memory
directory with a MEMORY.md index, or docs like ARCHITECTURE_NOTES.md), check
those too before writing a lesson that duplicates recorded knowledge.

If an existing lesson shares the root cause: bump `occurrences` (+1 per mined
session, not per repetition inside one session), append the new session to
`sessions:`, enrich `keywords:` with any new trigger phrasing, and (if the new
incident adds a nuance) extend the Strategy. Do NOT create a near-duplicate.

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
  but avoid terms so generic they'd fire on unrelated work. Wrapped lines are
  fine — indent continuations.>"
signal: user-correction | ci-failure | reverted-pr | test-fail | self-judged
occurrences: 1
helpful: 0
harmful: 0
sessions: [<transcript-basename>/L<start>-L<end>]
created: <UTC ISO8601, e.g. 2026-07-16T14:03:00Z — run `date -u +%Y-%m-%dT%H:%M:%SZ`>
tier: lesson            # the miner always writes "lesson"; /consolidate promotes
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
- **`sessions:` evidence format** is `<transcript-basename>/L<start>-L<end>`
  (JSONL line range of the incident).
- **`created` is the activation stamp and is IMMUTABLE.** Write it once, at
  creation, to the current UTC time. NEVER change it when bumping occurrences or
  editing the strategy — the dashboard splits before/after friction on this
  timestamp, so moving it would silently corrupt the "did it help?" measurement.
  (When you only *bump* an existing lesson, leave its `created` untouched.)
- **helpful/harmful counters — DO NOT bump these.** They are owned by the
  deterministic attributor (`scripts/attribute.py`, run automatically at
  SessionEnd), which correlates `injections.jsonl` with each session's
  transcript and bumps the counters from code. If you also bumped them here you
  would DOUBLE-COUNT. You may *read* `~/.claude/flywheel/state/events.jsonl`
  (the attributor's output) for context on which lessons have been helping, but
  leave the `helpful:`/`harmful:` frontmatter integers alone.

## Step 6 — Record events + report

For every lesson you **write or bump-occurrence**, append one line to
`~/.claude/flywheel/state/events.jsonl` — the metrics source
`/flywheel:consolidate` reads. Use ONLY the `add` / `bump-occurrence` ops here;
`bump-helpful` / `bump-harmful` events are written by the deterministic
attributor, not by you (see Step 5):

```json
{"ts": <unix>, "op": "add|bump-occurrence", "lesson": "<id>", "class": "<class>", "scope": "<scope>", "session": "<transcript-basename>"}
```

End with a compact summary: lessons written (id, class, scope, signal),
lessons bumped (id, new occurrences), candidates you deliberately skipped as
trivia or disconfirmed on reading, and — if any lesson looks procedural and
recurring — flag it as a `/flywheel:consolidate` promotion candidate. If
mining `--queued`, mark processed queue entries `"mined": true`.
