---
name: recall
description: >
  Check the flywheel's learned lessons before debugging or fixing. Use when
  investigating a bug, hitting unexpected/intermittent behavior, about to
  propose a fix for a non-trivial problem, or when the current symptom feels
  like something that may have happened before. Also on "/flywheel:recall" or
  "did we learn anything about this?".
argument-hint: "[symptom or topic]"
---

# /flywheel:recall — pull the relevant learned lesson

You are the retrieval step of the flywheel's PULL channel. A lesson index may
already be in context from SessionStart (`[flywheel] Lesson index — ...`); this
skill is the deliberate version of the same move, for when work has evolved past
the opening prompt.

## Step 1 — Search by the DIAGNOSED problem, not the user's words

Form the query from what you now know about the problem (symptom class, layer,
error text), then search both tiers:

```bash
grep -ril "<term1>\|<term2>" <project-root>/.claude/lessons/ ~/.claude/flywheel/lessons/ 2>/dev/null
```

Good query terms are symptom-shaped: "intermittent", "swallowed", "CORS",
"schema", "soft delete", "not showing" — not generic words. If `$ARGUMENTS`
was given, use it as the starting query.

## Step 2 — Read and apply (or explicitly pass)

- **Read the matching lesson file(s) in full** — the Read is logged as usage and
  its outcome is scored, so only Read a lesson you intend to seriously consider.
- Apply the **Strategy** section to the current problem. It is advisory: if on
  reading it clearly doesn't fit, say so and move on (that non-fit is signal
  too — do not force it).
- If nothing matches, say "no learned lesson covers this" and continue normally.
  If the current problem later produces a hard-won insight, that's a
  `/flywheel:learn` candidate at session end.

## Rules

- At most 2 lessons pulled per problem — precision over coverage.
- Never pull for non-code work (session management, scheduling, chat).
- Do NOT edit lesson files here (counters are owned by the deterministic
  attributor; content changes belong to /flywheel:learn and /flywheel:consolidate).
