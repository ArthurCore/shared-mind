# DEV-096 candidate response contract integrity — TDD evidence

## RED

Six test methods were added before implementation. The initial run produced
**18 failures and 1 error**. Unknown top-level/private fields, ignored nested
fields, missing/invalid fields, and malformed evaluator expected responses were
accepted; a non-mapping candidate raised raw `AttributeError`.

Executable probes confirmed both `raw_prompt` at the top level and `confidence`
under a settled claim retained a `100/100`, passing report.

## GREEN

The scorer now loads and caches the pinned Draft 2020-12 response schema and
applies it to both evaluator expected responses and candidate responses before
semantic comparison. Stable errors expose path and schema keyword only.

```text
DEV-096 focused: 6/6 PASS
evaluation regression: 39/39 PASS
```

## Full regression

Python 3.13.2 parallel branch coverage completed with:

```text
TOTAL files=59 tests=476 failures=0 seconds=33.786
branch-enabled coverage total 83%
```

Both contract validators, compileall, Ruff, configured mypy, Bandit,
dependency audit, and `git diff --check` passed.

## Self-dogfooding

Canonical Proposals created
`workitem_dev_096_candidate_response_integrity_001` and moved it from `TODO`
version 1 to `DOING` version 2 at ledger sequences 188 and 189.

The valid golden fixture remained passing. Five hostile candidates failed
closed with `INVALID_CANDIDATE_RESPONSE`: top-level `raw_prompt`, nested claim
confidence, malformed proposition hash, resolved-conflict status, and terminal
work-item status.

The immutable task trace
`trace:dev-096-candidate-response-integrity-20260815-001` was captured as
source revision `revision_dfc48187a429d58daf5e133dd462fae0`. Re-capture
returned `UNCHANGED`, proving idempotent preservation. One malformed closeout
Proposal was durably rejected with `SCHEMA_VALIDATION_FAILED`; the corrected
canonical Proposal then moved the WorkItem to `DONE` version 3 at ledger
sequence 191.

Final local integrity evidence:

```text
kernel state root: sha256:0bb2ccdfc760d36bbe83086364c6b53d4f01ffb2a7676ca782cc992240ed29e4
kernel head:       sha256:a1bc820a03066fdd48497955f35897bc237103237717fd3d7dd9f71bc4f52d2d
kernel replay:     valid=true, checked_entries=191
product audit:     sha256:3b1eaa20d67f42bf8620a8d800a83ab3d9943c5625a79398cb43f401f88ed611
product verify:    PRODUCT_INTEGRITY_VALID
next context:      sha256:7d318c224b513ceabd1577968d196219e152a4aba2418f4c3c2a14a291bdebaa
```

PR #15 first source/test/documentation head
`62f360ee3b96ba516e878399ac793c0ea7184c60` passed all eight jobs in hosted
[run 31875521479](https://github.com/ArthurCore/shared-mind/actions/runs/31875521479):
Python 3.11, 3.12, and 3.13 contract/coverage; Linux, macOS, and Windows
determinism; quality/security; and fresh base/MCP wheel smoke.
