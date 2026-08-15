# DEV-093 evaluation input integrity — TDD evidence

## RED

Six test methods were added before implementation. They produced **19 failures
and 1 error** across 20 malformed-input cases:

```text
offline empty quality: no exception
offline missing quality field: raw KeyError
offline version/threshold/bool/NaN: no exception
live string/number/null passed: no exception
live bool/out-of-range/NaN/string quality: no exception
```

The empty offline quality case returned quality-preserved because Python's
`all()` over an empty iterable is true. The live path converted non-boolean
values with `bool()` and numeric-looking values with `int()`/`float()`.

## GREEN

The scorer now validates the complete metrics/report boundary before comparison
and raises stable error prefixes. Results:

```text
DEV-093 focused: 6/6 PASS
evaluation continuity regression: 29/29 PASS
```

## Full regression

Python 3.13.2 parallel branch coverage completed with:

```text
TOTAL files=56 tests=457 failures=0 seconds=33.014
branch-enabled coverage total 83%
```

Both contract validators, compileall, Ruff, configured mypy, Bandit,
dependency audit, and `git diff --check` passed.

## Self-dogfooding

The same `../shared-mind-memory` state was verified after DEV-092. Canonical
Proposals created `workitem_dev_093_evaluation_input_integrity_001` and moved it
from `TODO` version 1 to `DOING` version 2 at ledger sequences 176 and 177.

An offline dogfood probe confirmed valid report v2 still passes and the
historical comparison v1 remains equal to the checked-in artifact. Four hostile
probes returned their expected prefixes:

```text
offline empty quality -> INVALID_OFFLINE_QUALITY_METRICS
offline NaN threshold -> INVALID_OFFLINE_REDUCTION_THRESHOLD
live string passed -> INVALID_LIVE_REPORT_PASSED
live NaN quality -> INVALID_LIVE_REPORT_QUALITY
```

The immutable trace
`trace:dev-093-evaluation-input-integrity-20260815-001` was captured into the
same Shared State as source revision
`revision_f3821cf015538359cdbeee7194a2ce05` at ledger sequence 178. A guarded
Proposal moved the WorkItem from `DOING` version 2 to `DONE` version 3 at ledger
sequence 179.

Final closeout evidence:

```text
kernel state root: sha256:332bb3bcaea1a11e9c661c6c1ff62105ceabf3eb5d04519854de2e8608cc5e94
kernel head: sha256:8f13ee570f936cffcb1fee41fe27443e0c3867809d27d8bdd05f36aa7bd9c0fc
product audit head: sha256:bd7e7407fa26972a72b0e5de2bb6e073317cfd83812264fda19324b8f79bcdab
next context hash: sha256:86b43a54d3403f67fd51020c9766b4f12d87d4b7e29e26cbfcfd5d3996cb735e
```

`shared-mind replay --verify`, product consolidation, and
`PRODUCT_INTEGRITY_VALID` all passed. The next task-aware context contains no
active WorkItem or OpenQuestion.
