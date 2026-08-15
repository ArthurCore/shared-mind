# DEV-089 schema 1.3 benchmark certification — TDD evidence

## RED

The initial six tests fixed the API, strict result schema, current-schema gate,
verify-before-replay order, replay parity, deterministic hash, and no-clobber
writer.  Production had neither the module nor the schema:

```console
PYTHONPATH=src python3 -m unittest tests.test_benchmark_certification -v
# 6 tests: 1 failure, 5 errors
```

Checkpoint: `adac8c8 test: define DEV-089 benchmark certification`.

After the harness was GREEN, a seventh test required both real fresh 100k
artifacts.  It failed only for the missing `hot-active` result:

```text
profile='hot-active': AssertionError: False is not true
```

Checkpoint: `2288564 test: require fresh schema 1.3 benchmark evidence`.

## GREEN

`benchmarks.certify_100k` now creates, verifies, explicitly replays, measures,
validates, hashes, and atomically writes one profile.  The schema is
`context-benchmark-certification@1` and does not permit undeclared fields.

```console
PYTHONPATH=src python3 -m unittest -v \
  tests.test_benchmark_certification \
  tests.test_projection \
  tests.test_projection_performance_contract \
  tests.test_benchmark_fixture \
  tests.test_benchmark_performance_contract
# 28 tests, 0 failures
```

Implementation checkpoint: `c84e9e6 feat: add current-schema benchmark certification`.
Fresh evidence checkpoint: `c7298ce test: certify fresh schema 1.3 at 100k entries`.

## Actual 100k evidence

Both profiles were generated from an empty file by current schema 1.3 code on
Python 3.13.2, SQLite 3.45.3, macOS 15.0.1 arm64.  Each used five warm-ups,
fifty measured samples, and a 32,000-byte hard limit.

| Profile | Ledger / receipts | Verify | Replay parity | Context p95 | Bytes | Result |
|---|---:|---:|---|---:|---:|---|
| history-heavy | 100,000 / 100,000 | 100,000, 0 errors | exact | 4.7165 ms | 950 | PASS |
| hot-active | 100,000 / 100,000 | 100,000, 0 errors | exact | 1.6767885 s | 2,928 | PASS |

The source and replay databases are deliberately outside Git.  The checked-in
JSON retains their SHA-256 digests, sizes, snapshots, raw samples, environment,
timings, and certification self-hash without retaining machine-local paths.

## Full regression and dogfooding

Python 3.13.2 parallel branch-coverage regression:

```text
TOTAL files=53 tests=439 failures=0 seconds=32.447
branch-enabled coverage total 83%
```

Both contract validators, compileall, Ruff, the configured mypy scope, Bandit,
local Markdown-link validation, and `git diff --check` passed.  The immutable
task trace `trace:dev-089-schema13-benchmark-20260815-001` was captured through
the public product CLI as source revision
`revision_047636427d74d7f29c9ce3e3626b9aff`.  A guarded kernel Proposal then
moved `workitem_dev_089_schema13_benchmark_001` from `DOING` v2 to `DONE` v3 at
ledger sequence 163.  Final consolidation and verification returned
`PRODUCT_INTEGRITY_VALID`, state root
`sha256:314a8933114c35c4425019761275baeb670bf729e7a87284c8e0d0a58f60fea5`,
and head hash
`sha256:8005d43b9e79b4ca782ec1adbd3ec347a691006780725c2f24e07caac1b78b58`.

The next-session SUMMARY context used the captured source as an explicit
reference, reported no active WorkItems or open Questions, and produced context
hash `sha256:b582284a85944a651553c6313a908ea752c08daf1d147b26e7f06d88f1d86e5a`.
Result files remain derived benchmark evidence and do not mutate canonical
state.

Hosted evidence for PR #8's first documentation head:

- PR run
  [`31871756255`](https://github.com/ArthurCore/shared-mind/actions/runs/31871756255):
  all eight CI jobs PASS;
- push run
  [`31871738857`](https://github.com/ArthurCore/shared-mind/actions/runs/31871738857):
  all eight CI jobs PASS;
- CodeQL run
  [`31871755515`](https://github.com/ArthurCore/shared-mind/actions/runs/31871755515):
  actions and Python analysis PASS.
