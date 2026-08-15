# DEV-095 scenario grounding integrity — TDD evidence

## RED

Seven test methods were added before implementation. They produced **25
failures** across vacuous scenario, version/schema/shape drift, identity and
purpose drift, empty dimensions, and ungrounded records.

The decisive executable probe was:

```text
context = {}
expected_response = {}
response = {scenario_id: atlas-continuity-golden-v1}
result = score 100/100, fact accuracy 1.0, conflict recall 1.0, passed true
```

## GREEN

The scorer now validates the complete scenario@1 boundary and semantic
context-to-expected-response grounding before comparing a candidate response.
All invalid paths use `INVALID_SCENARIO_CONTRACT`.

```text
DEV-095 focused: 7/7 PASS
evaluation regression: 33/33 PASS
```

## Full regression

Python 3.13.2 parallel branch coverage completed with:

```text
TOTAL files=58 tests=470 failures=0 seconds=34.820
branch-enabled coverage total 83%
```

Both contract validators, compileall, Ruff, configured mypy, Bandit,
dependency audit, and `git diff --check` passed.

## Self-dogfooding

Canonical Proposals created
`workitem_dev_095_scenario_grounding_integrity_001` and moved it from `TODO`
version 1 to `DOING` version 2 at ledger sequences 184 and 185.

The valid golden fixture retained 100/100 quality. Five hostile probes failed
closed with `INVALID_SCENARIO_CONTRACT`: vacuous context/expected response,
context scenario-ID drift, purpose drift, settled-claim hash drift, and omitted
conflict member.

The immutable trace
`trace:dev-095-scenario-grounding-integrity-20260815-001` was captured into the
same Shared State as source revision
`revision_c42014a7329727d3a8647a65541de209` at ledger sequence 186. A guarded
Proposal moved the WorkItem from `DOING` version 2 to `DONE` version 3 at ledger
sequence 187.

Final closeout evidence:

```text
kernel state root: sha256:73a5587aeaa02610f3ac6d01a49450f5b24c350870167c3926ebdb8862b18508
kernel head: sha256:fe5a1366853ce5298ba365d7f4b43a578edcfd8c0872854d62391283b56a634b
product audit head: sha256:4cc22455b6ede91a94cf19229a6147f54c522fd6ba2824934d709d2fbe26885d
next context hash: sha256:e91be092288c29d5d293972dedcadfbd15a89a47873bfacd7653a19a62292602
```

`shared-mind replay --verify`, product consolidation, and
`PRODUCT_INTEGRITY_VALID` all passed. The next task-aware context contains no
active WorkItem or OpenQuestion. Hosted CI evidence is appended after the PR
head passes all eight jobs.
