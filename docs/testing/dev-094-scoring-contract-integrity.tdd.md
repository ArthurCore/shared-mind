# DEV-094 scoring contract integrity — TDD evidence

## RED

Six test methods were added before implementation. They produced **16 failures
and 2 errors** across weakened thresholds, exact typed constants, dimension
weights, penalties, and field-set cases:

```text
weakened passing/quality thresholds: no exception
changed/extra/boolean dimension weights: no exception
missing dimension: raw KeyError
changed/missing/extra/boolean penalties: no exception
changed/boolean/NaN score and quality constants: no exception
missing scoring field: raw KeyError
```

An executable probe also showed that a malformed response with most required
dimensions absent could pass after the evaluator-side thresholds were lowered.

## GREEN

The scorer now validates the evaluator-side scoring contract before computing
dimension matches, penalties, reductions, or a pass decision. It requires the
exact typed constants, dimensions, and penalties and raises
`INVALID_SCORING_CONTRACT` for drift.

```text
DEV-094 focused: 6/6 PASS
evaluation regression: 35/35 PASS
```

## Full regression

Python 3.13.2 parallel branch coverage completed with:

```text
TOTAL files=57 tests=463 failures=0 seconds=34.192
branch-enabled coverage total 83%
```

Both contract validators, compileall, Ruff, the configured mypy scope, Bandit,
dependency audit, and `git diff --check` passed.

## Self-dogfooding

The same `../shared-mind-memory` state was verified before implementation.
Canonical Proposals created `workitem_dev_094_scoring_contract_integrity_001`
and moved it from `TODO` version 1 to `DOING` version 2 at ledger sequences 180
and 181.

An offline dogfood probe confirmed the valid golden response still passes with
score `100`, fact accuracy `1.0`, and conflict recall `1.0`. Four hostile
scoring cases returned the expected stable prefix:

```text
weakened passing threshold -> INVALID_SCORING_CONTRACT
dimension weight drift -> INVALID_SCORING_CONTRACT
negative penalty drift -> INVALID_SCORING_CONTRACT
NaN quality threshold -> INVALID_SCORING_CONTRACT
```

The immutable trace
`trace:dev-094-scoring-contract-integrity-20260815-001` was captured into the
same Shared State as source revision
`revision_1f4b60dd2fabe9b3dd92390f8481924d` at ledger sequence 182. A guarded
Proposal moved the WorkItem from `DOING` version 2 to `DONE` version 3 at ledger
sequence 183.

Final closeout evidence:

```text
kernel state root: sha256:a6067ab3bc1abbede1fe10c68c585a7d41f15bee92eb58b8ab521e0fc689db27
kernel head: sha256:93c6aea392d48f929fa48d081904f59dfd975389e0770caecf421fdbaac76fea
product audit head: sha256:dcb520873a777f808553042a6e1b0e77825815e43b34c53babd350d91e0d5913
next context hash: sha256:11f1f825db999b8fe8900442142584056ece07c832853d27083738fd196c4a7b
```

`shared-mind replay --verify`, product consolidation, and
`PRODUCT_INTEGRITY_VALID` all passed. The next task-aware context contains no
active WorkItem or OpenQuestion.

## Hosted evidence

PR #13 source/test/documentation head
`3051f9986ac6e867cb6ef4949a609fc161e3e616` passed
[GitHub Actions run 31874440698](https://github.com/ArthurCore/shared-mind/actions/runs/31874440698):
Python 3.11, 3.12, and 3.13 full contract/coverage, Ubuntu/macOS/Windows
determinism, quality/security, and fresh wheel smoke all succeeded.
