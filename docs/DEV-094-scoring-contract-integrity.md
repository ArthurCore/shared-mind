# DEV-094 — Scoring contract integrity

## Problem

The deterministic product-continuity scorer validated response and resource
inputs, but trusted the evaluator-side `scoring` object. A caller could lower
the passing score and required quality thresholds, change dimension weights,
or make penalties negative. A response with poor continuity accuracy could
therefore pass without changing the canonical scenario identity or response
schema.

## Contract

- The scoring object has one exact field set.
- `maximum_score` and `passing_score` are typed integer constants equal to 100.
- Required fact accuracy and open-conflict-member recall are typed finite float
  constants equal to `1.0`.
- The six dimension names and weights are exact and total 100 points.
- The three penalty names and positive values are exact.
- Booleans are never accepted as numeric scoring constants.
- Missing, extra, mistyped, non-finite, weakened, or otherwise changed values
  raise the stable `INVALID_SCORING_CONTRACT` prefix before scoring.
- Valid report v2 output and explicit historical report v1 behavior remain
  unchanged.

## Acceptance

- A bad response cannot pass by weakening thresholds.
- Weight and penalty omissions, additions, or value drift fail closed.
- Boolean and NaN constants fail closed.
- The golden scenario still produces score `100/100`, fact accuracy `1.0`,
  conflict recall `1.0`, and `passed=true`.
- Targeted tests, full branch coverage, same-state dogfooding, immutable task
  trace capture, final replay/product verification, and hosted CI pass.

RED/GREEN and closeout evidence is recorded in
[`testing/dev-094-scoring-contract-integrity.tdd.md`](testing/dev-094-scoring-contract-integrity.tdd.md).
