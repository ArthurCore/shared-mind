# DEV-090 streaming benchmark evidence — TDD evidence

## RED

Three tests were added before implementation:

```console
PYTHONPATH=src python3 -m unittest -v \
  tests.test_benchmark_certification.BenchmarkCertificationTest.test_database_evidence_hashes_with_bounded_streaming_reads \
  tests.test_benchmark_certification.BenchmarkCertificationTest.test_database_evidence_rejects_a_file_changed_during_hashing \
  tests.test_benchmark_certification.BenchmarkCertificationTest.test_database_evidence_rejects_non_regular_files
```

Result: **3 tests, 2 failures, 1 error**. The previous implementation triggered
the test's whole-file-read assertion, accepted a file changed during hashing,
and leaked raw `IsADirectoryError` for a directory.

Checkpoint: `d3dea5f test: require bounded benchmark evidence hashing`.

## GREEN

The evidence helper now hashes from one descriptor with 1MiB reads, validates a
regular-file descriptor, and compares before/after descriptor metadata plus the
observed byte count.

```text
tests.test_benchmark_certification: 10/10 PASS
benchmark/projection targeted regression: 31/31 PASS
Ruff, py_compile, git diff --check: PASS
```

Checkpoint: `a2adb58 fix: stream benchmark database evidence hashes`.

## Real 503MiB evidence parity

The preserved DEV-089 hot-active source and replay databases were hashed again
with the streaming implementation and compared to the checked-in certification:

| File | Bytes | SHA/size parity | Streaming elapsed |
|---|---:|---|---:|
| source | 527,572,992 | exact | 277.319 ms |
| replay | 527,572,992 | exact | 278.605 ms |

The first two-file pass took 556.004ms. `tracemalloc` peak was 2,105,158 bytes
and `/usr/bin/time -l` maximum RSS was 39,534,592 bytes; memory no longer scales
with the 503MiB file size. These timing/memory figures are environment-specific,
while SHA/size parity and fixed read size are deterministic contract evidence.

## Closeout

Python 3.13.2 parallel branch-coverage regression completed with:

```text
TOTAL files=53 tests=442 failures=0 seconds=33.111
branch-enabled coverage total 83%
```

Both contract validators, compileall, Ruff, configured mypy, Bandit, local
Markdown links, and `git diff --check` passed. Immutable trace
`trace:dev-090-streaming-hash-20260815-001` was captured through the public CLI
as source revision `revision_8950dd5572f7854076835797799afb0d`. A guarded
Proposal moved `workitem_dev_090_streaming_hash_001` from `DOING` v2 to `DONE`
v3 at ledger sequence 167. Final consolidation and verification returned
`PRODUCT_INTEGRITY_VALID`, state root
`sha256:093de6b6beee70bcfe20bcec73569b661f6c9f10a160dd111d83f3c73133ab1d`,
and head hash
`sha256:8445dd3c870ad882ba2a44e6328e3bf4f74f6ddb12699cee08abcc3e468d4014`.
The next-session context has no active WorkItem or open Question and hash
`sha256:81fb08ce082136efefb722f0035c678bd5b402660fd6c2bf93100e290d6d305c`.

Hosted evidence for PR #9's first head:

- PR run [`31872311320`](https://github.com/ArthurCore/shared-mind/actions/runs/31872311320):
  all eight CI jobs PASS;
- push run [`31872301244`](https://github.com/ArthurCore/shared-mind/actions/runs/31872301244):
  all eight CI jobs PASS;
- CodeQL run [`31872310697`](https://github.com/ArthurCore/shared-mind/actions/runs/31872310697):
  actions and Python analysis PASS.
