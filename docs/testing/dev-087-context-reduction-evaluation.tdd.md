# DEV-087 context reduction evaluation — TDD and dogfooding evidence

## Restored Shared State

The session recovered current state from the real `../shared-mind-memory`
workspace.  `shared-mind-product verify` returned `PRODUCT_INTEGRITY_VALID`.
The remaining open question was
`question_extract_ed93cd0df2102e15488f3287`: how far context can be reduced
while preserving correct next-action selection.

A validated kernel Proposal created
`workitem_dev_087_context_reduction_001` as `TODO` at ledger sequence 152.  An
initial attempt to create it directly as `DOING` failed contract validation,
which demonstrated the lifecycle guard.  A second guarded Proposal moved it
from `TODO` v1 to `DOING` v2 at ledger sequence 153 and state root
`sha256:e5daf8ce2b3c7ed4c2125e32c86bd761a71862bbd373d7da6193704a60f28aec`.

## RED → GREEN

1. RED: `tests/test_paired_context_reduction.py` could not import the planned
   immutable paired runner or evaluator.
2. GREEN: same-state paired scoring, exact reductions, strict threshold
   validation, quality-regression checks, and stable failure codes were added.
3. GREEN: the versioned report schema, immutable no-clobber evidence runner,
   and ProductService/CLI/MCP parity were added.
4. GREEN: 11 DEV-087 tests and 25 combined continuity tests passed.

## Actual self-dogfooding measurement

Both arms used the same task, query, explicit references, selector version, and
kernel state.  The only intentional differences were depth and budget.

| Metric | Full baseline | Compact candidate | Reduction |
|---|---:|---:|---:|
| UTF-8 context bytes | 65,526 | 24,555 | 62.526325% |
| estimated tokens (`ceil(bytes/4)`) | 16,382 | 6,139 | 62.525943% |
| median context-ready latency (9 warm calls) | 646.564375 ms | 333.897209 ms | 48.358242% |

Both observations scored continuity accuracy, decision recall, open-question
recall, conflict recall, and evidence traceability at `1.0`; wrong-memory and
missing-critical-memory rates were `0.0`.  The compact candidate reduced
irrelevant-context rate from `0.869565` to `0.625`.  The paired report passed
the declared 60% byte/token and 40% time thresholds with
`quality_preserved=true`.

The time metric is explicitly context-ready latency used as a deterministic
productive-action proxy, not model inference latency.  The token count is the
existing deterministic estimator, not a provider tokenizer.

Immutable evidence:

- safe `evals/shared_state_continuity/fixtures/dev-087-*.v1.json` inputs and raw
  timing samples (full context bodies are intentionally not retained because
  source evidence locators can contain workstation-local absolute paths)
- `evals/shared_state_continuity/results/dev-087-self-dogfood.v1.json`

## Regression and closeout

Python 3.13 used the repository parallel branch-coverage runner:

```text
test files:       51
tests:            428
failures:         0
branch coverage:  83%
elapsed:          32.446s
```

Both contract validators, compileall, Ruff, the configured mypy scope, Bandit,
and `git diff --check` passed.  The first runner invocation used a system
Python 3.13 without the `coverage` package and therefore executed zero tests;
the correctly provisioned project validation environment produced the result
above.  This was an environment-selection error, not a product failure.

## Shared Mind closeout

The strict task trace
`trace:dev-087-context-reduction-20260815-001` was captured as immutable source
revision `revision_f73009ace502d83a928214114483d23d`.  Its six ordered events
retain TASK, TOOL, RESULT, DECISION, FAILURE, and TEST evidence, including the
RED commit, actual measurements, and full regression.  The first capture command
used an out-of-workspace temporary path and was independently rejected with
`PATH_OUTSIDE_WORKSPACE`; moving the unchanged trace input inside the selected
workspace allowed the canonical capture.

A single guarded kernel Proposal then answered
`question_extract_ed93cd0df2102e15488f3287` with the measured local scope and
moved `workitem_dev_087_context_reduction_001` from `DOING` v2 to `DONE` v3.
Final consolidation and verification returned `PRODUCT_INTEGRITY_VALID` at
ledger sequence 155, state root
`sha256:86a23bfa35cd34d3d629968b65edfd5cb7834294e5f3cd5fc07062815a229fa3`,
and next-session context hash
`sha256:81a6ccc38860e51319bf5909ebd9d4295467648f63f821bd4a9ed227c5015e65`.
There are no `OPEN` questions or `TODO`, `DOING`, or `BLOCKED` WorkItems.

Evaluation artifacts remain evidence and do not mutate canonical truth.
