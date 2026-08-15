# DEV-092 unclamped offline reduction — TDD evidence

## RED

Four tests were written before implementation:

```console
PYTHONPATH=src python3 -m unittest tests.test_offline_reduction_integrity -v
```

Result: **4 tests, 2 failures, 2 errors**. The scorer always returned
`product-continuity-report@1`, accepted no version argument, and the scenario
still pinned the v1 schema.

## GREEN

The scorer now defaults to report version 2, dispatches explicit version 1
compatibility, validates all resource inputs before division, and uses a new
strict v2 schema. A fifth test locks the nested live-summary report dispatch.

```text
DEV-092 focused tests: 5/5 PASS
offline + live product-continuity regression: 23/23 PASS
```

## Full regression

Python 3.13.2 parallel branch coverage completed with:

```text
TOTAL files=55 tests=451 failures=0 seconds=32.759
branch-enabled coverage total 83%
```

Both executable contract validators, Ruff, py_compile, JSON schema parsing,
and `git diff --check` passed.

## Self-dogfooding

The session verified the same `../shared-mind-memory` state after DEV-091,
created `workitem_dev_092_unclamped_offline_reduction_001` by canonical
Proposal, and moved it to `DOING` with version/status guards. A local offline
dogfood run used manual `100/100/10s` and context `125/150/12.5s`: report v1
validated with zero reductions, while report v2 validated with
`-0.25/-0.5/-0.25` and correctly failed the reduction gate.

The immutable closeout trace
`trace:dev-092-unclamped-offline-reduction-20260815-001` was captured into the
same Shared State as source revision
`revision_e6ea61e5665c8daca73c3a7b829e377b` at ledger sequence 174. The
version/status-guarded closeout Proposal then moved the WorkItem from `DOING`
version 2 to `DONE` version 3 at ledger sequence 175.

Final verification evidence:

```text
kernel state root: sha256:79478db242e3f583644252af8107272c053c6925de317b195ea05cb2a77cf39f
kernel head:       sha256:6777b38e499e058cf79d868d890b9c3cc452d621c349dbbc90f186ca2a70954c
product audit head: sha256:71c04ad09de6bc5176aec7aa06a24135a044aa2e4aa5c4de6c1e1ed8b0649279
next context hash: sha256:93226262eebe3453e47b2be6a834b6229c07ce31803de7aa88f55c4baf6fa66f
```

`shared-mind replay --verify`, `shared-mind-product consolidate`, and
`shared-mind-product verify` all passed. The next-session context was generated
from the same state with a task-aware 8,192-token budget; the initial 4,096
budget failed closed because the mandatory continuity payload did not fit.
