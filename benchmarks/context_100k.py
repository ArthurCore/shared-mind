"""Opt-in DEV-021 runner for 100k-ledger context latency."""

from __future__ import annotations

import argparse
import json
import math
import platform
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Sequence

from shared_mind.canonical import canonical_json, sha256_bytes
from shared_mind.projection import build_context_pack

from .fixture_builder import build_benchmark_fixture


BENCHMARK_VERSION = "dev-021-context-benchmark@1"
TARGET_P95_NS = 2_000_000_000


def summarize_latencies_ns(samples: Sequence[int]) -> dict[str, int | float]:
    """Summarize nanosecond samples with the nearest-rank p95 definition."""

    if not samples:
        raise ValueError("at least one latency sample is required")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in samples
    ):
        raise ValueError("latency samples must be non-negative integer nanoseconds")
    ordered = sorted(samples)
    rank = math.ceil(len(ordered) * 0.95)
    return {
        "sample_count": len(ordered),
        "min_ns": ordered[0],
        "median_ns": statistics.median(ordered),
        "p95_ns": ordered[rank - 1],
        "max_ns": ordered[-1],
    }


def run_context_benchmark(
    database: str | Path,
    *,
    warmups: int = 5,
    samples: int = 50,
    budget_bytes: int = 32_000,
) -> dict[str, Any]:
    """Measure warm-filesystem library latency without fixture construction."""

    values = (
        ("warmups", warmups),
        ("samples", samples),
        ("budget_bytes", budget_bytes),
    )
    for name, value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    path = Path(database)
    if not path.is_file():
        raise FileNotFoundError(path)

    expected_digest: str | None = None
    rendered_bytes: int | None = None
    for _ in range(warmups):
        pack = build_context_pack(
            path,
            budget_bytes=budget_bytes,
            purpose="Measure deterministic DEV-021 context generation.",
        )
        expected_digest, rendered_bytes = _validate_pack(
            pack, budget_bytes, expected_digest
        )

    timings: list[int] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        pack = build_context_pack(
            path,
            budget_bytes=budget_bytes,
            purpose="Measure deterministic DEV-021 context generation.",
        )
        elapsed = time.perf_counter_ns() - started
        expected_digest, rendered_bytes = _validate_pack(
            pack, budget_bytes, expected_digest
        )
        timings.append(elapsed)

    summary = summarize_latencies_ns(timings)
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "metric": "warm-filesystem-library-context",
        "clock": "time.perf_counter_ns",
        "p95_method": "nearest-rank",
        "warmup_count": warmups,
        "budget_bytes": budget_bytes,
        "context_sha256": expected_digest,
        "context_rendered_bytes": rendered_bytes,
        "target_p95_ns": TARGET_P95_NS,
        "target_met": summary["p95_ns"] <= TARGET_P95_NS,
        "latency": summary,
        "samples_ns": timings,
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
    }


def _validate_pack(
    pack: dict[str, Any], budget_bytes: int, expected_digest: str | None
) -> tuple[str, int]:
    encoded = canonical_json(pack).encode("utf-8")
    if len(encoded) > budget_bytes:
        raise RuntimeError("context exceeded the hard byte budget")
    digest = sha256_bytes(encoded)
    if expected_digest is not None and digest != expected_digest:
        raise RuntimeError("context output changed between benchmark samples")
    return digest, len(encoded)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--ledger-entries", type=int, default=100_000)
    parser.add_argument(
        "--profile", choices=("history-heavy", "hot-active"), default="history-heavy"
    )
    parser.add_argument("--seed", type=int, default=21)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--budget-bytes", type=int, default=32_000)
    arguments = parser.parse_args(argv)

    fixture_manifest = None
    if arguments.create:
        fixture_manifest = build_benchmark_fixture(
            arguments.database,
            ledger_entries=arguments.ledger_entries,
            profile=arguments.profile,
            seed=arguments.seed,
        )
    result = run_context_benchmark(
        arguments.database,
        warmups=arguments.warmups,
        samples=arguments.samples,
        budget_bytes=arguments.budget_bytes,
    )
    result["fixture"] = fixture_manifest
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
