# DEV-092 — Unclamped offline evaluation reductions

## Problem

After DEV-091 fixed the sanitized live-comparison envelope, the deterministic
offline product-continuity scorer still clamped resource regressions to zero in
`metric_comparison`. Changing the formula inside `product-continuity-report@1`
would silently change a pinned report contract, so the fix requires an explicit
report version boundary.

## Contract

- `product-continuity-report@2` is the default scorer output and uses signed
  `1 - context / baseline` reductions.
- `product-continuity-report@1` remains available through an explicit argument
  and preserves its historical zero-to-one clamp.
- The golden scenario pins `product-continuity-report.schema.v2.json`.
- The sanitized live-summary envelope accepts both nested report versions, but
  rejects negative reductions mislabeled as report version 1.
- Unknown versions and boolean, zero, negative, or non-finite resource metrics
  fail closed with stable error prefixes.

The score dimensions, conflict recall, hallucination penalties, quality gate,
response schema, and historical sanitized live artifacts are unchanged.

## Acceptance

- A regression fixture emits `-0.25 / -0.5 / -0.25` in report version 2.
- The same fixture emits `0 / 0 / 0` in explicit report version 1.
- Both reports validate against their own strict schema.
- Nested live-summary validation dispatches by report version.
- Targeted tests, full branch coverage, actual dogfooding, task trace capture,
  and final Shared Mind verification pass.

RED/GREEN and closeout evidence are recorded in
[`testing/dev-092-unclamped-offline-reduction.tdd.md`](testing/dev-092-unclamped-offline-reduction.tdd.md).
