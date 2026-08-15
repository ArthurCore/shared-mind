# DEV-093 — Evaluation input integrity

## Problem

The offline and live product-continuity helpers previously assumed callers had
already run JSON Schema validation. A malformed offline scenario with empty
quality maps passed `all([])` as quality-preserved, while a live report with the
string `"false"` was truthy when converted with `bool()`. Non-finite and
out-of-range quality values were also accepted.

## Contract

- Offline metrics must use `product-continuity-metrics@1`, its exact field set,
  the pinned 0.5 threshold, exact resource fields, and exact quality fields.
- Resource measurements remain finite and positive.
- Quality measurements are numbers, not booleans, are finite, and lie in
  `[0, 1]`.
- Live nested reports must use supported report versions, a real boolean
  `passed`, an integer score in `[0, 100]`, and finite bounded quality values.
- Invalid inputs raise stable `INVALID_OFFLINE_*` or `INVALID_LIVE_*` prefixes.
- Valid report v2 output and checked-in historical v1 comparison output remain
  unchanged.

## Acceptance

- Empty/missing/extra offline quality fields cannot vacuously pass.
- Metric version and threshold drift fail closed.
- Boolean, NaN, infinity, negative, and above-one quality values fail closed.
- Truthy-string live pass flags and invalid nested report versions fail closed.
- Targeted regression, full branch coverage, same-state dogfooding, task-trace
  capture, final verification, and hosted CI pass.

Evidence is recorded in
[`testing/dev-093-evaluation-input-integrity.tdd.md`](testing/dev-093-evaluation-input-integrity.tdd.md).
