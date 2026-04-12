# Scoring

The public output exposes one score: `final_score`.

Internally:

- `model_score`
  Measures how well the candidate matches the task, the user's rubric, and the
  "more human / less template-like" objective.
- `rule_score`
  Measures hard constraints and low-level penalties:
  - min/max length
  - required phrase coverage
  - banned phrase hits
  - common template-phrase hits
  - formatting penalties

Default formula:

```text
final_score = 0.78 * model_score + 0.22 * rule_score
```

This is intentionally simple for V1 so users can understand why a draft was
kept or discarded.

The default scorer model is:

- `BAAI/bge-reranker-v2-m3`

It is used as a local reranker:

- query = task + default-or-custom goal + constraints + "human-like Chinese message" rubric
- document = candidate message

Higher score means the candidate fits the target rubric better.

## Hard Fail Conditions

A candidate is considered unsafe to keep when:

- `must_include` exists and any required phrase is missing
- `max_chars` exists and the draft is much longer than allowed
- the draft contains too many banned phrases

## Why This Is Not An AI Detector

The score does not try to answer:

- "Was this written by AI?"

It tries to answer:

- "How well does this candidate fit the user's desired human Chinese communication style?"
