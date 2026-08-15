"""Deterministic, no-clobber runner for DEV-082 and DEV-086 evidence."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from shared_mind.canonical import canonical_json, sha256_bytes, sha256_json
from shared_mind.continuity_eval import (
    benchmark_context_quality,
    evaluate_paired_context_reduction,
    evaluate_zero_relearning,
)


MAX_INPUT_BYTES = 16 * 1024 * 1024


def run_evaluation(
    context_path: str | Path,
    observation_path: str | Path,
    expectation_path: str | Path,
    output_path: str | Path,
    *,
    elapsed_ms: int | float,
    token_count: int,
) -> dict[str, Any]:
    """Evaluate immutable inputs and atomically publish one result artifact."""

    context = _read_object(context_path)
    observation = _read_object(observation_path)
    expectation = _read_object(expectation_path)
    zero = evaluate_zero_relearning(
        context,
        observation,
        expectation,
        elapsed_ms=elapsed_ms,
        token_count=token_count,
    )
    quality = benchmark_context_quality(
        context,
        observation,
        expectation,
        elapsed_ms=elapsed_ms,
        token_count=token_count,
    )
    result = {
        "artifact_version": "shared-state-continuity-run@1",
        "input_hashes": {
            "context": sha256_json(context),
            "observation": sha256_json(observation),
            "expectation": sha256_json(expectation),
        },
        "zero_relearning": zero,
        "context_quality": quality,
        "passed": zero["passed"] and quality["passed"],
    }
    _publish_no_clobber(Path(output_path), (canonical_json(result) + "\n").encode("utf-8"))
    return result


def run_paired_evaluation(
    baseline_context_path: str | Path,
    baseline_observation_path: str | Path,
    candidate_context_path: str | Path,
    candidate_observation_path: str | Path,
    expectation_path: str | Path,
    thresholds_path: str | Path,
    output_path: str | Path,
    *,
    baseline_elapsed_ms: int | float,
    candidate_elapsed_ms: int | float,
    baseline_token_count: int,
    candidate_token_count: int,
) -> dict[str, Any]:
    """Evaluate one baseline/candidate pair and atomically retain the evidence."""

    documents = {
        "baseline_context": _read_object(baseline_context_path),
        "baseline_observation": _read_object(baseline_observation_path),
        "candidate_context": _read_object(candidate_context_path),
        "candidate_observation": _read_object(candidate_observation_path),
        "expectation": _read_object(expectation_path),
        "thresholds": _read_object(thresholds_path),
    }
    report = evaluate_paired_context_reduction(
        documents["baseline_context"],
        documents["baseline_observation"],
        documents["candidate_context"],
        documents["candidate_observation"],
        documents["expectation"],
        documents["thresholds"],
        baseline_elapsed_ms=baseline_elapsed_ms,
        candidate_elapsed_ms=candidate_elapsed_ms,
        baseline_token_count=baseline_token_count,
        candidate_token_count=candidate_token_count,
    )
    result = {
        "artifact_version": "paired-context-reduction-run@1",
        "input_hashes": {
            name: sha256_json(document) for name, document in documents.items()
        },
        "report": report,
        "passed": report["passed"],
    }
    _publish_no_clobber(
        Path(output_path), (canonical_json(result) + "\n").encode("utf-8")
    )
    return result


def _read_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    with source.open("rb") as stream:
        encoded = stream.read(MAX_INPUT_BYTES + 1)
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError(f"evaluation input exceeds {MAX_INPUT_BYTES} bytes: {source}")
    try:
        parsed = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON evaluation input: {source}") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError(f"evaluation input must be an object: {source}")
    return dict(parsed)


def _publish_no_clobber(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise FileExistsError(f"evaluation evidence is immutable: {path}")
    digest = sha256_bytes(payload).split(":", 1)[1]
    temporary = path.with_name(f".{path.name}.{digest}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise FileExistsError(f"evaluation evidence is immutable: {path}")
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shared-state-continuity-eval")
    parser.add_argument("context")
    parser.add_argument("observation")
    parser.add_argument("expectation")
    parser.add_argument("output")
    parser.add_argument("--elapsed-ms", type=float, required=True)
    parser.add_argument("--token-count", type=int, required=True)
    args = parser.parse_args(argv)
    result = run_evaluation(
        args.context,
        args.observation,
        args.expectation,
        args.output,
        elapsed_ms=args.elapsed_ms,
        token_count=args.token_count,
    )
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
