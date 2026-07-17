# Lesson schema (v2)

One file per lesson. Project tier: `<project>/.claude/lessons/<id>.md` ·
Global tier: `~/.claude/flywheel/lessons/<id>.md`.

```markdown
---
id: kebab-case-slug                  # unique; also the filename
class: unverified-assumption | skipped-ground-truth | wrong-path |
       missed-prime-suspect | rework-loop | missing-context | win
scope: global | project              # global = would help in ANY codebase
symptom: "how the problem announces itself, in words a user would type"
keywords: "8–15 comma-separated trigger terms incl. synonyms — this is the
  retrieval surface for automatic injection (lexical matching, no embeddings);
  use the words a frustrated user actually types: 'not showing', 'flaky',
  'sometimes', 'still broken'"
signal: user-correction | ci-failure | reverted-pr | test-fail | self-judged
occurrences: 1                       # bumped on recurrence — the promotion signal
helpful: 0                           # bumped when an injected lesson helped
harmful: 0                           # bumped when an injected lesson misled
sessions: [transcript-or-session/loc]  # evidence pointers
tier: lesson | skill-candidate       # /flywheel:consolidate promotes
status: active | promoted | retired
---
**Strategy (retrieved/injected):** The GENERALIZED, transferable decision
rule, written as advice to a future agent facing the symptom. Self-contained,
3–10 lines. This block is what the hook injects — nothing below it is.

**Incident (evidence):** What actually happened: the wrong move, its cost
(rounds of rework), the fix. 2–6 lines. Audit trail only; never injected.
```

## The three rules that keep the store useful

1. **Strategy ≠ incident.** An incident log helps only on identical repeats;
   a strategy transfers. Abstract upward: "trust the live store over repo
   artifacts", not "labs.deleted_at was missing".
2. **Signal honesty.** Real outcome signals (user corrections, CI, reverted
   PRs) outrank self-judgment. Record which one backs the lesson.
3. **Keywords are the API.** Injection is lexical; a lesson with poor keywords
   is a lesson that never fires. Generous synonyms, no ultra-generic terms.
