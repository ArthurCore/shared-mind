# DEV-082~086 continuity evaluations — TDD and dogfooding evidence

## Restored Shared State

The session started from the real sibling workspace `../shared-mind-memory`, not
from a user-supplied history. `shared-mind-product verify` returned
`PRODUCT_INTEGRITY_VALID`. The task-aware context restored the project purpose,
One Shared State, the Agent Loadout removal decision, Core Context's derived
status, the project and Skill mutation boundaries, the active DEV-082 WorkItem,
and its source evidence.

The start transition used a kernel Proposal. It moved DEV-082 from `TODO` v1 to
`DOING` v2 at ledger sequence 149 and state root
`sha256:3027b43940910ed61577c9921533487e11f4e16e82d39bb497572dea1a562408`.
No evaluator wrote canonical state.

## RED → GREEN

1. RED: `tests/test_continuity_evaluation.py` failed because
   `shared_mind.continuity_eval` did not exist.
2. GREEN: deterministic zero-relearning, pollution, lifecycle, conflict, and
   context-quality evaluators were added.
3. RED: immutable runner/schema, context-hash tamper detection, and interface
   parity were absent.
4. GREEN: a strict report schema, no-clobber runner, ProductService/CLI/MCP
   parity, and fail-closed context integrity checks were added.
5. GREEN: 15 targeted tests and 51 related regression tests passed.

The retained evidence is
`evals/shared_state_continuity/results/dev-082-086-self-dogfood.v1.json`.
Its inputs and report are content-hashed; changing an existing run ID to
different bytes is rejected.

## Full regression

Python 3.13 branch-coverage execution used the repository's parallel coverage
runner:

```text
test files:       50
tests:            417
failures:         0
branch coverage:  82%
elapsed:          32.356s
```

Both kernel and product contract validators passed. `compileall`, Ruff, the
configured mypy scope, Bandit, and `git diff --check` passed.

## Actual self-dogfooding measurements

| DEV | Result |
|---|---|
| DEV-082 | continuity/decision/question/conflict/evidence recall all `1.0`; wrong and missing-critical rates `0.0`; irrelevant context `2/21 = 0.095238`; 65,516 bytes; 16,379 estimated tokens; 157,000 ms to the first RED commit |
| DEV-083 | duplicate, irrelevant, stale, wrong, and confidently-wrong cases all detected; the deliberately polluted input correctly reports `passed=false` |
| DEV-084 | 180 records: CURRENT 176, STALE 2, SUPERSEDED 0, COMPLETED 2; historical records preserved |
| DEV-085 | real canonical conflict resolved at ledger sequence 4; original-claim preservation `1.0`; partition/rationale/evidence checks and four-entry ledger verification passed |
| DEV-086 | relevant recall `1.0`, evidence traceability `1.0`, missing-critical `0.0`, irrelevant `0.095238`; benchmark passed its declared thresholds |

The token count is explicitly the deterministic
`ceil(UTF-8 bytes / 4)` estimator, not a model tokenizer. The live Shared Mind
workspace had no open conflict, so conflict recall was vacuously complete; the
mutation workflow was separately exercised against an isolated real Kernel
workspace rather than fabricating a conflict in canonical project memory.

## Authority and reproducibility

- Evaluation reports are derived evidence, never authoritative truth.
- Factual/project mutations use only kernel Proposals.
- Skill mutations remain on the ProductMutationProposal boundary.
- No Agent-, client-, or model-specific canonical memory was introduced.
- The same state/request/version produces the same report and hash.

## Shared Mind closeout

The immutable trace
`trace:dev-082-086-continuity-evaluations-20260815-001` was captured as source
revision `revision_fb82c9a31c7aa1cff73c941be355cd56`. A single validated kernel
Proposal then marked DEV-082~086 `DONE` and answered the zero-relearning,
wrong-memory, and lifecycle questions with that source as evidence. The
context-reduction question remains intentionally `OPEN`: this run established
absolute context cost but did not produce a paired reduction baseline.

Final consolidation and verification returned `PRODUCT_INTEGRITY_VALID` at
ledger sequence 151 and state root
`sha256:9c0555aeb0ba9cd35fadc3b17005f7bb201230b0cfb0afd6587f3c03110c686a`.
There are no `TODO`, `DOING`, or `BLOCKED` WorkItems. A next-session context was
generated with context hash
`sha256:50f0b8aea2982917d6455ae03419e5f3426406da8f34d11327e84e2d9fb9e317`.
