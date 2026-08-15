# DEV-098 — Evaluator Policy and Adversarial Contract Integrity

## Problem

The product-continuity scenario already pinned its top-level field set, but
`execution_policy` and `adversarial_cases` were not interpreted by the public
scorer. A secret-bearing policy and arbitrary ineffective vectors could remain
inside a scenario that still received 100/100 and `passed=true`.

## Requirements

- `execution_policy` MUST use the exact offline-golden policy: network disabled
  in tests, live client disabled by default, explicit opt-in required, and the
  opt-in environment variable pinned.
- Adversarial cases MUST be a non-empty list of closed records with stable
  unique names and unique supported penalty codes.
- Every adversarial response MUST satisfy the same closed candidate schema and
  use the scenario's identity.
- Every declared case MUST trigger exactly its declared penalty under the same
  penalty computation used for ordinary reports.
- Malformed, private, ineffective, duplicate, or mismatched vectors MUST fail
  with `INVALID_SCENARIO_CONTRACT` before a pass decision is returned.
- The valid golden report and each existing adversarial result MUST remain
  exact.

## Contract and compatibility

No schema/version change is required. This closes evaluator-side semantic
validation in the existing `product-continuity-scenario@1` contract. It does
not enable live access or add a provider client; the scorer remains offline and
deterministic.
