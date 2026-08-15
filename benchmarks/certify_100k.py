"""Reproducible current-schema context benchmark certification harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json

from .context_100k import BENCHMARK_VERSION, run_context_benchmark
from .fixture_builder import FIXTURE_VERSION, build_benchmark_fixture


CERTIFICATION_VERSION = "context-benchmark-certification@1"
DATABASE_HASH_CHUNK_BYTES = 1_048_576
ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "contracts" / "atlas-predicate-registry.v1.json"
SCHEMA_PATH = ROOT / "benchmarks" / "context-benchmark-certification.schema.v1.json"


class CertificationError(Exception):
    """Stable fail-closed benchmark certification error."""

    def __init__(self, code: str, message: str, *, data: Any | None = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


def certify_context_benchmark(
    source_database: str | Path,
    replay_database: str | Path,
    *,
    ledger_entries: int,
    profile: str,
    seed: int,
    implementation_id: str,
    warmups: int = 5,
    samples: int = 50,
    budget_bytes: int = 32_000,
) -> dict[str, Any]:
    """Create and certify one fresh current-schema benchmark fixture.

    Fixture generation, ledger verification, explicit file replay, and context
    timing are one invocation.  Database paths are intentionally excluded from
    the result so identical content is portable across machines and workdirs.
    """

    source_path = Path(source_database).resolve()
    replay_path = Path(replay_database).resolve()
    if source_path == replay_path:
        raise ValueError("source and replay database paths must differ")
    if replay_path.exists():
        raise FileExistsError(f"replay database already exists: {replay_path}")
    if not isinstance(implementation_id, str) or not implementation_id.strip():
        raise ValueError("implementation_id must be a non-empty string")
    if len(implementation_id.encode("utf-8")) > 256:
        raise ValueError("implementation_id must be at most 256 UTF-8 bytes")

    generation_started = time.perf_counter_ns()
    fixture = build_benchmark_fixture(
        source_path,
        ledger_entries=ledger_entries,
        profile=profile,
        seed=seed,
    )
    generation_elapsed = time.perf_counter_ns() - generation_started
    current_schema = Kernel.SUPPORTED_VERSIONS["schema"]
    if fixture["schema_version"] != current_schema:
        raise CertificationError(
            "SCHEMA_VERSION_MISMATCH",
            "Fresh fixture schema does not match the current write schema.",
            data={"expected": current_schema, "actual": fixture["schema_version"]},
        )
    if int(fixture["ledger_entries"]) != ledger_entries:
        raise CertificationError(
            "FIXTURE_LEDGER_COUNT_MISMATCH",
            "Fresh fixture ledger count differs from the requested count.",
        )

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    kernel = Kernel(source_path, registry)
    replayed: Kernel | None = None
    try:
        source_snapshot = _kernel_snapshot(kernel)
        verification_started = time.perf_counter_ns()
        verification_raw = kernel.verify_ledger()
        verification_elapsed = time.perf_counter_ns() - verification_started
        verification = {
            "valid": bool(verification_raw["valid"]),
            "checked_entries": int(verification_raw["checked_entries"]),
            "head_hash": verification_raw["head_hash"],
            "state_root": verification_raw["state_root"],
            "errors": list(verification_raw["errors"]),
        }
        if (
            not verification["valid"]
            or verification["errors"]
            or verification["checked_entries"] != ledger_entries
            or verification["head_hash"] != fixture["head_entry_hash"]
            or verification["state_root"] != fixture["state_root"]
        ):
            raise CertificationError(
                "LEDGER_VERIFICATION_FAILED",
                "Fresh fixture failed ledger verification or manifest parity.",
                data=verification,
            )

        replay_started = time.perf_counter_ns()
        replayed = kernel.replay(replay_path)
        replay_elapsed = time.perf_counter_ns() - replay_started
        target_snapshot = _kernel_snapshot(replayed)
        replay_parity = source_snapshot == target_snapshot
        if not replay_parity:
            raise CertificationError(
                "REPLAY_PARITY_MISMATCH",
                "Explicit replay differs from the certified source snapshot.",
                data={"source": source_snapshot, "target": target_snapshot},
            )
    finally:
        if replayed is not None:
            replayed.close()
        kernel.close()

    context_started = time.perf_counter_ns()
    context = run_context_benchmark(
        source_path,
        warmups=warmups,
        samples=samples,
        budget_bytes=budget_bytes,
    )
    context_elapsed = time.perf_counter_ns() - context_started

    result: dict[str, Any] = {
        "certification_version": CERTIFICATION_VERSION,
        "implementation_id": implementation_id.strip(),
        "profile": profile,
        "requested_ledger_entries": ledger_entries,
        "schema_version": current_schema,
        "projection_version": Kernel.SUPPORTED_VERSIONS["projection"],
        "fixture_version": FIXTURE_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "fixture": fixture,
        "databases": {
            "source": _database_evidence(source_path),
            "replay": _database_evidence(replay_path),
        },
        "timings_ns": {
            "generation": generation_elapsed,
            "verification": verification_elapsed,
            "replay": replay_elapsed,
            "context": context_elapsed,
        },
        "verification": verification,
        "replay": {
            "parity": replay_parity,
            "source": source_snapshot,
            "target": target_snapshot,
        },
        "context": context,
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "certified": bool(context["target_met"]),
    }
    result["certification_hash"] = sha256_json(result)
    _validate_result(result)
    return result


def write_certification_result(result: Mapping[str, Any], output: str | Path) -> None:
    """Atomically create a canonical, immutable result file without clobbering."""

    normalized = dict(result)
    _validate_result(normalized)
    content = (canonical_json(normalized) + "\n").encode("utf-8")
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _kernel_snapshot(kernel: Kernel) -> dict[str, Any]:
    head = kernel.connection.execute(
        "SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    schema_versions = [
        str(row[0])
        for row in kernel.connection.execute(
            "SELECT DISTINCT schema_version FROM receipts "
            "WHERE schema_version IS NOT NULL ORDER BY schema_version"
        )
    ]
    return {
        "ledger_count": int(
            kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        ),
        "receipt_count": int(
            kernel.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        ),
        "head_sequence": int(head["seq"]) if head is not None else 0,
        "head_hash": head["entry_hash"] if head is not None else None,
        "state_root": kernel.state_root(),
        "receipt_schema_versions": schema_versions,
    }


def _database_evidence(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise CertificationError(
                    "DATABASE_NOT_REGULAR",
                    "Benchmark database evidence must be a regular file.",
                )
            while chunk := handle.read(DATABASE_HASH_CHUNK_BYTES):
                digest.update(chunk)
                size_bytes += len(chunk)
            after = os.fstat(handle.fileno())
    except CertificationError:
        raise
    except IsADirectoryError as exc:
        raise CertificationError(
            "DATABASE_NOT_REGULAR",
            "Benchmark database evidence must be a regular file.",
        ) from exc
    except OSError as exc:
        raise CertificationError(
            "DATABASE_EVIDENCE_UNAVAILABLE",
            "Benchmark database could not be read for evidence hashing.",
        ) from exc

    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    before_identity = tuple(getattr(before, field) for field in identity_fields)
    after_identity = tuple(getattr(after, field) for field in identity_fields)
    if before_identity != after_identity or size_bytes != after.st_size:
        raise CertificationError(
            "DATABASE_CHANGED_DURING_HASH",
            "Benchmark database changed while evidence was being hashed.",
        )
    return {
        "sha256": f"sha256:{digest.hexdigest()}",
        "size_bytes": size_bytes,
    }


def _validate_result(result: Mapping[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(dict(result))
    claimed = result.get("certification_hash")
    payload = {key: value for key, value in result.items() if key != "certification_hash"}
    if claimed != sha256_json(payload):
        raise CertificationError(
            "CERTIFICATION_HASH_MISMATCH",
            "Certification hash does not match canonical result bytes.",
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--replay-database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--implementation-id", required=True)
    parser.add_argument("--ledger-entries", type=int, default=100_000)
    parser.add_argument(
        "--profile", choices=("history-heavy", "hot-active"), default="history-heavy"
    )
    parser.add_argument("--seed", type=int, default=89)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--budget-bytes", type=int, default=32_000)
    arguments = parser.parse_args(argv)

    result = certify_context_benchmark(
        arguments.database,
        arguments.replay_database,
        ledger_entries=arguments.ledger_entries,
        profile=arguments.profile,
        seed=arguments.seed,
        implementation_id=arguments.implementation_id,
        warmups=arguments.warmups,
        samples=arguments.samples,
        budget_bytes=arguments.budget_bytes,
    )
    write_certification_result(result, arguments.output)
    print(canonical_json(result))
    return 0 if result["certified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CERTIFICATION_VERSION",
    "DATABASE_HASH_CHUNK_BYTES",
    "CertificationError",
    "certify_context_benchmark",
    "main",
    "write_certification_result",
]
