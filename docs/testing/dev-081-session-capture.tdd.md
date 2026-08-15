# DEV-081 Real Session Capture TDD evidence

## Restored starting state

The session did not rely on a user-supplied project-history summary. It opened
the real external workspace at `../shared-mind-memory`, verified it, requested
task-aware EVIDENCE context, and inspected the current Scenario, Core Context,
WorkItem, Decisions, Questions, and bootstrap source.

| Evidence | Restored value |
|---|---|
| Start integrity | `PRODUCT_INTEGRITY_VALID` |
| Kernel sequence | 145 before starting DEV-081 |
| Start state root | `sha256:badd5f246fc4ffc0f1b325f9c77e2b47b7203a06ebef906736654e6bb8a46ce4` |
| Task context hash | `sha256:4d753ca88697bb32c3e44116f6b1c84f36f3ceac444a511413f3019118dbf3eb` |
| Active work | `workitem_extract_aa0a56c4dbee6f1a7734bd3c` (`DEV-081`, P0) |
| Work Scenario | `artifact_scenario-work-35b64b5f2c525223` |
| Core Context | `artifact_core-project`, `authoritative=false` |
| Bootstrap source | `revision_f409eb0fcd6020a05cd4c159fc5d9569` |

The restored Decisions explicitly required One Shared State, task-aware
context instead of Agent Loadout, disposable Core/Scenario projections, the
kernel Proposal boundary for factual/project state, and the product proposal
boundary for shared Skill state. The workspace had four open Questions and no
open conflicts.

DEV-081 was moved from `TODO` version 1 to `DOING` version 2 through the public
kernel Proposal `proposal_start_dev_081_session_capture_001`; direct SQLite
mutation was not used. Consolidation and product verification then passed.

## Acceptance

The versioned trace contract and user journeys are documented in
[`../DEV-081-real-session-capture.md`](../DEV-081-real-session-capture.md).
The acceptance suite covers:

- strict TASK/TOOL/RESULT/DECISION/FAILURE/TEST events;
- stable trace identity and exact duplicate idempotency;
- immutable conflict rejection;
- malformed JSON/schema, task mismatch, duplicate event ID, and sequence gap;
- atomic publish failure;
- post-registration retry without duplicate ledger history;
- exact timestamp and order preservation;
- fresh-service search and source-span drill-down;
- CLI parity and workspace path containment.

## RED and GREEN

| Stage | Command | Result |
|---|---|---|
| RED | `PYTHONPATH=src python -m unittest -v tests.test_task_trace_capture tests.test_product_contract` | 13 tests; 12 failure reports and 4 errors. Malformed traces created six ledger/source rows, identity replacement was accepted, atomic publish was absent, and no trace contract/receipt existed. |
| Core GREEN | Same focused command plus `tests.test_product_governance_eval` | 23/23 PASS. |
| CLI GREEN | `PYTHONPATH=src python -m unittest -v tests.test_product_interfaces tests.test_task_trace_capture tests.test_product_contract` | 20/20 PASS. |
| Kernel contract | `python contracts/validate_contract.py` | PASS: 7 predicates, 16 typed, 6 negative, 6 semantic, 7 continuity operations. |
| Product contract | `python contracts/validate_product_contract.py` | PASS: 10 typed fixtures, 14 negative cases. |

## Full regression and quality

All commands ran with Python 3.13.2.

| Gate | Result |
|---|---|
| Parallel branch coverage | 49 test modules, 401 tests, 0 failures, 82% total branch coverage |
| Compileall | PASS |
| Shipped-source Ruff | PASS |
| Configured mypy surface | PASS |
| Bandit medium/high | PASS |
| Strict dependency audit | PASS, no known vulnerabilities |
| `git diff --check` | PASS |

The full coverage command was:

```bash
PYTHONPATH=src python tools/run_parallel_coverage.py \
  --workers 2 --timeout 2400 --fail-under 80
```

## Actual self-dogfooding capture

The implementation session was captured through the new public CLI into the
same external Shared State:

```bash
shared-mind-product capture DEV-081 \
  captures/dev-081-session-20260815-001.json
```

| Field | Value |
|---|---|
| Trace ID | `trace:dev-081-implementation-20260815-001` |
| Capture status | `CAPTURED` |
| Events | 6, all six event types |
| Batch | `batch_c1eeaa0f9bc207fd7174c135` |
| Source revision | `revision_7a93e8238afcd5dc8c9a94f14f3a8129` |
| Content hash | `sha256:8fda9c0094b4e0a27ab5aef685abcf9dcc7e1a4a6492a2f02bf82802fd9d7243` |
| Extraction | `COMPLETED`, zero drafts/failures |
| Post-capture kernel sequence | 147 |
| Post-capture state root | `sha256:f4d9212209cbf097cbd212a2bb1d13cd9aad11cd53d7dbe38f4df6d621164cac` |
| Integrity | `PRODUCT_INTEGRITY_VALID` |

A second CLI invocation returned `UNCHANGED`, the same revision ID, and an
empty changed-artifact list; kernel sequence and state root remained 147 and
`sha256:f4d921...64cac`. Read-only audit inspection found exactly one
`TASK_TRACE_CAPTURED` event for the trace ID.

A fresh CLI process retrieved the exact source bytes. `captured_at` remained
`2026-08-15T01:08:53Z`, source and excerpt hashes matched, and the six event
types remained in input order. A new EVIDENCE context explicitly included the
trace revision with reasons `explicit reference` and matching task terms:

- context hash: `sha256:5fdf44ba4945e2ebd1ccba7a609f4d5aa30543afeccf163461f416b07b012ffd`
- included bytes: 65,527
- selected trace revision: `revision_7a93e8238afcd5dc8c9a94f14f3a8129`

This is one shared project state. No Codex-, Claude-, GPT-, model-, or
session-specific canonical memory was created.

## Completion and next handoff

After capture, consolidation, and verification, the public kernel Proposal
`proposal_complete_dev_081_session_capture_001` moved DEV-081 from `DOING`
version 2 to `DONE` version 3 at ledger sequence 148. Final workspace values:

- state root: `sha256:6fd15e191e5df0d26f5afd1f265918cd479d86bb41ea0ca2019126dd46e1e715`
- head hash: `sha256:7e6f9027fe0d456e71b13d7a44620990ee25edd527beb4e4c5dc45817cecb37c`
- product verification: `PRODUCT_INTEGRITY_VALID`
- DEV-082 WorkItem: `workitem_extract_83c292866dfcfc397860b284`, `TODO` version 1
- next-session context hash: `sha256:4180ae248912c87592d9509fd44a3295fd2cb6aa0e3d12dd99b28ce7feed4924`

The next context ranks DEV-082 first and carries the active Decisions and
source pointers needed to begin Zero-Relearning Evaluation without a project
history prompt.
