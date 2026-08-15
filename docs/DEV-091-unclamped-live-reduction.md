# DEV-091 — Unclamped live evaluation reductions

## Problem

`live_summary_comparison()` historically clamped every resource reduction to
the range 0 through 1. A context arm that used more bytes or tokens, or took
longer than the manual baseline, was therefore recorded as `0.0` instead of a
negative value. The pass/fail flag remained false, but the retained evidence
hid the magnitude and direction of the regression.

## Contract

- `product-continuity-live-comparison@2` is the default comparison contract.
- Version 2 records `1 - context / baseline` rounded to 12 decimal places
  without a lower clamp. A 25% regression is therefore `-0.25`.
- Historical `product-continuity-live-comparison@1` remains available only by
  explicit version selection and keeps its original clamp. Existing sanitized
  artifacts remain byte-identical and exactly reproducible.
- The live-summary envelope schema accepts either nested comparison version and
  applies version-specific reduction constraints.
- Unknown comparison versions and boolean, zero, negative, or non-finite
  resource metrics fail closed with stable error prefixes.

This changes derived evaluation evidence only. It does not mutate Shared State,
the kernel ledger, product state, or historical live artifacts.

## Acceptance

- A slower and more expensive context arm exposes negative byte, token, and
  time reductions in comparison version 2.
- The same input explicitly evaluated as version 1 reproduces the historical
  zero clamp.
- Both comparison versions validate against the sanitized live-summary schema.
- The two checked-in provider summaries retain their exact bytes and exact
  version-1 comparison documents.
- Targeted product-continuity tests, both contract validators, full branch
  coverage, and self-dogfooding verification pass.

RED/GREEN and closeout evidence are recorded in
[`testing/dev-091-unclamped-live-reduction.tdd.md`](testing/dev-091-unclamped-live-reduction.tdd.md).
