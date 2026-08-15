# DEV-095 — Scenario grounding integrity

## Problem

The deterministic scorer trusted the evaluator-side `context` and
`expected_response`. Setting both to empty objects and submitting only the
correct scenario ID made all six comparisons vacuously true, yielding
`100/100`, fact accuracy `1.0`, conflict recall `1.0`, and `passed=true`.

## Contract

- Scenario version, top-level field set, and response/metrics/report schema
  pins are exact for `product-continuity-scenario@1`.
- Context uses the exact scenario@1 field set, carries the same scenario ID,
  and has a non-missing, non-empty purpose.
- Expected response uses the exact response field set and the same scenario ID
  and purpose.
- Every scored list dimension is non-empty.
- Decisions, settled claims and evidence, open conflicts and every member,
  open questions, and actionable work items must be semantically grounded in
  the supplied context.
- Missing, extra, vacuous, drifted, or ungrounded scenario data raises the
  stable `INVALID_SCENARIO_CONTRACT` prefix before scoring.
- Valid golden output remains unchanged.

## Acceptance

- Empty context/expected-response fixtures cannot pass.
- Version, schema pin, scenario ID, and purpose drift fail closed.
- Empty dimensions and invented decisions, claims, conflicts, questions, or
  work state fail closed.
- The golden fixture still produces score `100/100`, fact accuracy `1.0`,
  conflict recall `1.0`, and `passed=true`.
- Targeted tests, full branch coverage, same-state dogfooding, immutable task
  trace capture, final replay/product verification, and hosted CI pass.

Evidence is recorded in
[`testing/dev-095-scenario-grounding-integrity.tdd.md`](testing/dev-095-scenario-grounding-integrity.tdd.md).
