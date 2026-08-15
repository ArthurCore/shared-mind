# DEV-091 unclamped live reduction — TDD evidence

## RED

Four tests were added before implementation:

```console
PYTHONPATH=src python3 -m unittest tests.test_live_reduction_integrity -v
```

Result: **4 tests, 1 failure, 3 errors**. The existing function always emitted
comparison version 1, had no explicit version parameter, and clamped all
negative reductions to zero.

## GREEN

The runner now defaults to `product-continuity-live-comparison@2`, retains
version 1 behind explicit selection, and rejects malformed metrics before
division. The live-summary schema dispatches reduction constraints by nested
comparison version.

```text
DEV-091 focused tests: 4/4 PASS
product-continuity evaluation regression: 18/18 PASS
```

The regression fixture uses manual metrics `100 bytes / 100 tokens / 10s` and
context metrics `125 bytes / 150 tokens / 12.5s`. Version 2 preserves
`-0.25 / -0.5 / -0.25`; version 1 returns its historical zero clamp. The
checked-in Codex artifact is read before and after recomputation to prove byte
stability.

## Full regression

Python 3.13.2 parallel branch coverage completed with:

```text
TOTAL files=54 tests=446 failures=0 seconds=33.159
branch-enabled coverage total 83%
```

Both executable contract validators passed. The first local coverage command
used a bare Python 3.13 interpreter without the `coverage` package and therefore
executed zero tests; it is recorded as an environment setup failure, not as
product evidence. The authoritative run used an isolated Python 3.13 quality
environment with the repository extras installed.

## Self-dogfooding

The session began by verifying `../shared-mind-memory`, requesting task-aware
context, confirming no active WorkItem or OpenQuestion, and drilling down to the
stored sanitized live evidence. A canonical Proposal created
`workitem_dev_091_unclamped_live_reduction_001` and a guarded update moved it to
`DOING` before implementation.

The eight-event immutable trace
`trace:dev-091-unclamped-live-reduction-20260815-001` was captured through the
public product CLI as source revision
`revision_d18f8ceaec2d03c7720ad54c7e724e3a`. A guarded Proposal then moved the
WorkItem from `DOING` v2 to `DONE` v3 at ledger sequence 171. Final
consolidation and verification returned `PRODUCT_INTEGRITY_VALID`, state root
`sha256:cdbfd6cde7f4e4f31d2f54ae27a9ec4bf0635efbdf0e69116159011644135917`,
ledger head
`sha256:e68cd5410349bffe1903df666e008529a7b96101d636046276ccf6bc2dec955d`,
and product audit head
`sha256:1c6665100bbdf697d42c6ae18efaeb6c6771e16c01cd0e5a0aa8623b2a15cfb4`.
The next-session context has no active WorkItem or OpenQuestion and hash
`sha256:b6ceed1c5dbf6c2b05a4a44d1d445a0f8e1e97cb550bf27707b0d042bb510fb7`.

## Hosted evidence

PR #10 implementation/documentation head
`13834d9835a8bb552e89339e05e638925625dc76` passed
[GitHub Actions run 31872892729](https://github.com/ArthurCore/shared-mind/actions/runs/31872892729):
Python 3.11, 3.12, and 3.13 full contract/coverage, Ubuntu/macOS/Windows
determinism, quality/security, and fresh wheel smoke all succeeded.
