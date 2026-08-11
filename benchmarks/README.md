# DEV-021 context benchmark

This opt-in benchmark measures Shared Mind context generation with a
deterministic, replay-valid ledger created exclusively through
`Kernel.commit()`. Fixture creation is intentionally outside the measured
region.

The default `history-heavy` profile updates one work item throughout the
ledger and finishes it before measurement. The `hot-active` profile leaves the
item actionable and exercises bounded history metadata in the context pack.

Run the 100k profile from the repository root:

```bash
PYTHONPATH=src python3 -m benchmarks.context_100k \
  /tmp/shared-mind-dev021.sqlite3 --create --ledger-entries 100000
```

Use `--profile hot-active` to reproduce the active-item stress profile. A new
fixture is written with the current schema, so it is not expected to reproduce
the historical schema-1.2 database hashes in the checked-in evidence.

The runner performs five warm-ups and fifty measured calls by default. It uses
`time.perf_counter_ns()` and reports nearest-rank p95, defined as
`sorted(samples)[ceil(0.95 * n) - 1]`. The current metric is explicitly named
`warm-filesystem-library-context`: it opens a fresh projection connection per
sample, benefits from the operating-system page cache, and excludes fixture
construction, terminal output, and CLI process startup.

Every sample must produce the same canonical context hash and remain within
the requested hard byte limit. The result records raw samples, Python,
SQLite, platform metadata, the two-second target, and whether that target was
met. Treat results as machine-specific; use a pinned runner for regression
gating rather than a wall-clock assertion in the normal unit suite.

The runner emits creation and context-timing evidence for one profile. The
checked-in combined `context-benchmark-result@2` also includes separately
captured verification, explicit replay, and final receipt-certification data;
that consolidated artifact is not emitted by one runner invocation.
