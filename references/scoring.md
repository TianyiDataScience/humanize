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
  - conservative, explainable writing-pattern clusters
  - formatting penalties

Default formula:

```text
final_score = 0.64 * model_score + 0.36 * rule_score
```

The formula stays intentionally simple so users can understand why a draft was
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

Writing-pattern audit findings never cause a hard fail by themselves.

## Writing Pattern Audit

Each scored candidate includes `writing_pattern_audit`, with the matched
categories, short evidence snippets, a rule score, and whether the pattern
cluster should enter the next repair round. The first version covers:

- inflated significance
- promotional language
- vague attribution
- formulaic contrast
- generic conclusion
- chatbot-style sign-off

The audit is conservative:

- One matched word or sentence pattern is visible but does not lower the score.
- A score penalty starts only when multiple signals form a cluster.
- Quoted text, titles, and fenced code are excluded from matching.
- The repair loop receives the matched categories as concrete instructions, so
  it can change the problem wording without flattening the rest of the copy.

## Why This Is Not An AI Detector

The score does not try to answer:

- "Was this written by AI?"

It tries to answer:

- "How well does this candidate fit the user's desired human Chinese communication style?"
