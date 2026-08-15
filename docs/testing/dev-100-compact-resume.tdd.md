# DEV-100 Compact Resume Context — TDD Evidence

## Source and user journeys

No separate plan file was used. The journeys came from canonical WorkItem
`workitem_dev_100_compact_resume_001`:

1. A new coding-agent session runs `shared-mind resume` and receives the
   continuity core in materially less context.
2. An evidence-heavy session explicitly requests the previous 128 KiB
   allowance.
3. A resume request above the safety ceiling fails at the CLI boundary and
   points to the advanced context command.

## RED

Command:

```console
PYTHONPATH=src python3 -m unittest tests.test_session_ux -v
```

Result: 8 tests ran with 3 intended failures. The production default was still
131,072 rather than 24,576 bytes, the end-to-end response exposed the same old
budget, and 131,073 was still accepted. The remaining five session tests
passed, including integrity-first fail-closed behavior. RED is preserved in
commits `45d864d` and `f187bdd`.

## GREEN

Production change: `DEFAULT_RESUME_BUDGET_BYTES` is 24,576 and the resume-only
argument validator accepts at most 131,072. The general `context` interface is
unchanged.

```console
PYTHONPATH=src python3 -m unittest \
  tests.test_session_ux tests.test_agent_bootstrap \
  tests.test_memory_views_product -v
```

Result: 19 tests, 0 failures. The minimal production GREEN is preserved in
commit `21e9222`.

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | Default resume task/depth remain task-aware EVIDENCE while the byte budget is 24 KiB | `test_resume_parser_has_safe_task_aware_defaults` | unit | PASS |
| 2 | Explicit 128 KiB evidence resume remains accepted | `test_resume_preserves_an_explicit_128_kib_evidence_budget` | unit | PASS |
| 3 | 128 KiB + 1 is rejected before workspace/context work | `test_resume_rejects_a_budget_above_the_128_kib_safety_ceiling` | unit | PASS |
| 4 | Repeated default resume is deterministic and restores purpose, active decision, open question, open conflict, actionable work, and projection references | `test_default_resume_restores_compact_continuity_and_drill_down_refs` | integration | PASS |
| 5 | Canonical serialized bytes respect the hard cap and core estimator tokens equal `ceil(bytes / 4)` | `test_default_resume_restores_compact_continuity_and_drill_down_refs` | integration | PASS |
| 6 | Invalid product integrity prevents context construction | `test_resume_fails_closed_before_context_when_integrity_is_invalid` | integration | PASS |

## Contract and quality gates

```console
python3 contracts/validate_contract.py
python3 contracts/validate_product_contract.py
```

Both Draft 2020-12 validators passed. Python 3.13.2 compileall, Ruff, configured
mypy, and Bandit also passed. Combined coverage from the full runner was 83%,
above the 80% branch threshold.

The managed sandbox prevented two environment-dependent gates from completing:

- full runner: 499 tests executed, 498 passed; the only error was the existing
  loopback smoke test receiving `PermissionError: [Errno 1] Operation not
  permitted` while binding `127.0.0.1:0`;
- `pip-audit --strict`: DNS/network access to `pypi.org` was unavailable.

The existing Python 3.13 venv also lacks `setuptools.build_meta`, so the local
wheel build could not start without installing a build dependency. No product
failure was observed in these three cases, but they are not recorded as PASS.
An unsandboxed or hosted gate remains required before canonical DONE closeout.

## Real self-dogfooding

Two fresh installed editable CLI invocations against `../shared-mind-memory`
returned identical results:

| Metric | Old explicit full | New default | Reduction |
|---|---:|---:|---:|
| Canonical context bytes | 130,997 | 24,527 | 81.2767% |
| Deterministic estimator tokens | 32,750 | 6,132 | 81.2763% |

Both default runs returned `SESSION_READY`, valid integrity, ledger sequence
205, state root
`sha256:9f9c11098ea48a2f7673c5ecaa666e9da1d46c0db71a8bf5f1f39b0a66feab96`,
and context hash
`sha256:3b2dfe5fc0c60ec08d36f9856a7c1c04aa35138b0cb21b6958bbfa38a8a2333a`.
The 16,096-byte core retained the project purpose, seven active decisions, the
active DEV-100 WorkItem, and its projection/source pointers. The workspace had
no open questions or conflicts to omit. Explicit `--budget-bytes 131072`
returned the prior 130,997-byte context, while 131,073 returned `USAGE_ERROR`.

## Merge evidence and known gap

- RED commits: `45d864d`, `f187bdd`
- GREEN commit: `21e9222`
- No commits were squashed or rewritten.
- The branch is locally implemented and dogfooded but remains blocked on the
  loopback/dependency-audit/build gates described above. It has not been pushed.
