# DEV-097 — Live Summary Contract Integrity

## Problem

`live_summary_comparison()` previously trusted callers to validate the
sanitized live-summary schema. A mapping containing only live arms plus
unknown or private fields could therefore produce `passed=true` even when the
artifact omitted provenance, version pins, redaction attestation, and provider
identity.

This is a public-boundary integrity defect: the checked-in schema was strict,
but the function that made the pass decision did not enforce it.

## Requirements

- The public comparison helper MUST validate the checked-in sanitized
  live-summary schema before returning a result.
- `comparison` MAY be absent while it is being calculated. If supplied, it
  MUST be schema-valid.
- Unknown top-level or nested fields, secret-bearing settings, missing
  provenance, malformed hashes/versions/identity, malformed arms, and invalid
  redaction attestations MUST fail closed with `INVALID_LIVE_SUMMARY`.
- Errors MUST expose only a structural path and schema keyword, never the
  rejected value.
- Existing metric and report validation codes MUST retain priority for the
  fields they already govern.
- Historical comparison `@1` and current comparison `@2` outputs MUST remain
  exact for valid checked-in evidence.

## Contract and compatibility

No schema version changes. The implementation reuses
`product-continuity-live-summary.schema.v1.json` as the single structural
authority and derives only a pre-comparison view where `comparison` is not a
required input. The output remains compatible with the full checked-in schema.

The scorer remains deterministic, offline, and free of provider or network
integration.
