# Kernel hardening TDD evidence

## Source and user journeys

No external plan file was used. The journeys came from the SRS-to-runtime audit.

1. As a proposing agent, I receive a structured validation receipt for malformed input instead of a Python or SQLite exception.
2. As a workspace owner, I cannot lose a Claim to a stale destructive supersede merely because the caller omitted a precondition.
3. As a reviewer, I do not see a fact conflict between a replacement Claim and the target that the same supersede operation makes inactive.
4. As a replay implementer, I can rely on every pinned semantic version being checked before commit.

## RED and GREEN evidence

| Stage | Command | Result |
|---|---|---|
| Initial RED | `PYTHONPATH=src python3 -m unittest tests.test_kernel_hardening -v` | 6 tests ran with 9 assertion failures and 1 raw `sqlite3.IntegrityError`; each failure reproduced an audited kernel defect. |
| Initial GREEN | `PYTHONPATH=src python3 -m unittest tests.test_kernel_hardening -v` | 6 tests passed. |
| Malformed-input RED | two targeted FR-011 unittest methods | Both errored: non-object input raised `AttributeError`; non-JSON input raised `TypeError`. |
| Malformed-input GREEN | the same two targeted FR-011 unittest methods | 2 tests passed. |
| Final regression | `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 12 tests passed. |
| Contract | `python3 contracts/validate_contract.py` | 7 predicates, 4 typed fixtures, 2 negative cases, and 2 semantic cases passed. |

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---:|---|---|---|---|
| 1 | Missing idempotency keys and unknown guards fail runtime Draft 2020-12 validation without a ledger append. | `test_fr_011_runtime_schema_*` | integration | PASS |
| 2 | Non-object and non-JSON proposals return machine-readable validation outcomes. | `test_fr_011_non_*` | integration | PASS |
| 3 | Duplicate object IDs roll back atomically and do not leave the SQLite connection inside a transaction. | `test_fr_011_duplicate_ids_are_normalized_and_rolled_back` | integration | PASS |
| 4 | Unsupported schema, registry, conflict-rule, guard-DSL, and projection versions are rejected. | `test_fr_015_rejects_every_unsupported_pinned_version` | integration | PASS |
| 5 | A destructive supersede without the target Claim aggregate read is rejected without mutation. | `test_fr_024_destructive_operation_requires_a_claim_version_read` | conformance | PASS |
| 6 | A supersede replacement is not compared for fact conflict against the target made inactive by that operation. | `test_fr_022_supersede_does_not_conflict_with_its_inactive_target` | conformance | PASS |
| 7 | Existing fact-conflict, stale-read, and idempotency behavior remains intact. | `tests/test_vertical_slice.py` | regression | PASS |

## Coverage and packaging

The standard-library trace coverage command was used because the environment does not contain the `coverage` package:

```text
shared_mind.canonical    100%
shared_mind.kernel        93%
shared_mind.validation    88%
```

`pip wheel` built `shared_mind_kernel-0.1.0-py3-none-any.whl` with `--ignore-requires-python` because the local interpreter is Python 3.10 while the project requires Python 3.11+. The wheel contains the runtime contract under `share/shared-mind/contracts/`, and a temporary isolated installation successfully constructed `Kernel` using that packaged schema.

## Known gaps

- The local environment has no Python 3.11 interpreter, `ruff`, `pyright`, `build`, or `coverage`; compile checks and wheel construction were used where possible.
- Global `pip check` reports unrelated pre-existing platform incompatibilities for `grpcio` and `torch`.
- Rejected idempotency-key reuse attempts still need an append-only receipt history separate from the idempotency mapping.
- Ledger-backed source registration, remaining mandatory operation guards, collection reads, replay, and continuity records remain future slices.

## Local checkpoint commits

- `3cb7a8c` — initial RED reproducers
- `353d13e` — initial GREEN implementation
- `b8ca4fb` — malformed-input RED boundaries
- `cf20800` — malformed-input GREEN implementation

These commits are local only; no remote push or merge was performed.
