# DEV-096 — Candidate response contract integrity

## Problem

The scorer enforced semantic grounding but did not itself apply the closed
candidate-response schema. A response could add top-level `raw_prompt`,
`api_key`, or private fields, or add ignored metadata under a settled claim,
and still receive `100/100` with `passed=true`. Non-mapping input leaked an
`AttributeError` instead of a stable validation failure.

## Contract

- Candidate and evaluator expected responses use the same pinned Draft
  2020-12 response schema.
- Unknown top-level and nested fields are rejected.
- Required fields, ID/hash patterns, non-empty summaries, evidence bounds,
  conflict/member cardinality and statuses, and actionable work statuses are
  checked before scoring.
- Candidate failures use `INVALID_CANDIDATE_RESPONSE`; malformed evaluator
  expected responses use `INVALID_SCENARIO_CONTRACT`.
- Error messages expose only the JSON path and failed schema keyword, never the
  submitted value.
- Valid and grounded summary paraphrases remain supported.

## Acceptance

- Unknown or secret-bearing fields cannot receive a passing report.
- Missing fields and non-object responses fail closed with stable errors.
- Invalid IDs, hashes, statuses, summaries, cardinality, and byte bounds fail
  closed before semantic scoring.
- The golden response and schema-valid grounded paraphrases still pass.
- Targeted tests, full branch coverage, same-state dogfooding, immutable trace
  capture, final replay/product verification, and hosted CI pass.

Evidence is recorded in
[`testing/dev-096-candidate-response-integrity.tdd.md`](testing/dev-096-candidate-response-integrity.tdd.md).
