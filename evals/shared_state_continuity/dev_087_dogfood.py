"""Build immutable DEV-087 paired-context evidence from one Shared State."""

from __future__ import annotations

import argparse
import tempfile
from math import ceil
from pathlib import Path
from statistics import median
from typing import Any, Sequence

from shared_mind.canonical import canonical_json
from shared_mind.product import ProductService

from .runner import _publish_no_clobber, run_paired_evaluation


TASK = (
    "Implement DEV-087 paired context reduction evaluation and close the "
    "context-reduction question"
)
QUERY = "context reduction next action one Shared State evidence WorkItem OpenQuestion"
REFERENCES = [
    "workitem_dev_087_context_reduction_001",
    "question_extract_ed93cd0df2102e15488f3287",
    "revision_fb82c9a31c7aa1cff73c941be355cd56",
]
PURPOSE = (
    "Preserve Shared Mind project reasoning and work state so any AI session "
    "can continue without re-explanation."
)
DECISION_IDS = [
    "decision_extract_bc334a3c1766687fbbcf6657",
    "decision_extract_2914c128b859b8c6d0d2c0d6",
    "decision_extract_abda017a680a99a91136c52b",
    "decision_extract_9bb5bdfd8b479b9a058d6a74",
    "decision_extract_fb62b6aa9763f9218533f515",
]
QUESTION_IDS = ["question_extract_ed93cd0df2102e15488f3287"]
WORK_ITEM_ID = "workitem_dev_087_context_reduction_001"
SOURCE_IDS = [
    "revision_f409eb0fcd6020a05cd4c159fc5d9569",
    "revision_fb82c9a31c7aa1cff73c941be355cd56",
]


def build_evidence(
    workspace: str | Path,
    output_root: str | Path,
    *,
    baseline_elapsed_ms: int | float,
    candidate_elapsed_ms: int | float,
    baseline_samples_ms: Sequence[float],
    candidate_samples_ms: Sequence[float],
) -> dict[str, Any]:
    """Build same-state contexts and publish a no-clobber paired report."""

    service = ProductService.open(workspace)
    try:
        common_request = {
            "task": TASK,
            "query": QUERY,
            "references": REFERENCES,
        }
        baseline = service.context(
            common_request | {"depth": "EVIDENCE", "budget_bytes": 65_536}
        )
        candidate = service.context(
            common_request | {"depth": "SUMMARY", "budget_bytes": 24_576}
        )
    finally:
        service.close()

    if baseline["kernel_state_root"] != candidate["kernel_state_root"]:
        raise RuntimeError("paired contexts were not built from the same kernel state")
    if (
        len(baseline_samples_ms) != 9
        or len(candidate_samples_ms) != 9
        or median(baseline_samples_ms) != baseline_elapsed_ms
        or median(candidate_samples_ms) != candidate_elapsed_ms
    ):
        raise ValueError("each elapsed value must be the median of exactly 9 samples")
    for identifier in [*DECISION_IDS, *QUESTION_IDS, WORK_ITEM_ID, *SOURCE_IDS]:
        if identifier not in canonical_json(baseline) or identifier not in canonical_json(
            candidate
        ):
            raise RuntimeError(f"critical context reference is missing: {identifier}")

    memory_truth = {
        "one-shared-state": "one canonical state; task-specific views",
        "agent-loadout": "removed because it fragments project memory",
        "core-context": "derived and non-authoritative",
        "project-boundary": "kernel Proposal and append-only ledger",
        "skill-boundary": "versioned ProductMutationProposal",
        "next-action": "complete DEV-087 paired context reduction evaluation",
    }
    critical_ids = [*DECISION_IDS, *QUESTION_IDS, WORK_ITEM_ID, *SOURCE_IDS]
    expectation = {
        "expectation_version": "zero-relearning-expectation@1",
        "purpose": PURPOSE,
        "decision_ids": DECISION_IDS,
        "open_question_ids": QUESTION_IDS,
        "conflict_ids": [],
        "active_work_item_id": WORK_ITEM_ID,
        "evidence_source_revision_ids": SOURCE_IDS,
        "critical_memory_ids": critical_ids,
        "relevant_context_ids": REFERENCES,
        "memory_truth": memory_truth,
        "thresholds": {
            "continuity_accuracy": 1.0,
            "decision_recall": 1.0,
            "open_question_recall": 1.0,
            "conflict_recall": 1.0,
            "evidence_traceability": 1.0,
            "wrong_memory_rate": 0.0,
            "missing_critical_memory_rate": 0.0,
            "irrelevant_context_rate": 0.9,
            "max_context_bytes": 70_000,
            "max_context_tokens": 20_000,
            "max_time_to_productive_action_ms": 2_000,
        },
    }
    observation = {
        "observation_version": "zero-relearning-observation@1",
        "purpose": PURPOSE,
        "decision_ids": DECISION_IDS,
        "open_question_ids": QUESTION_IDS,
        "conflict_ids": [],
        "active_work_item_id": WORK_ITEM_ID,
        "evidence_source_revision_ids": SOURCE_IDS,
        "memory_assertions": [
            {"memory_id": key, "value": value, "confidence": 1.0}
            for key, value in memory_truth.items()
        ],
    }
    thresholds = {
        "threshold_version": "paired-context-reduction-thresholds@1",
        "min_context_bytes_reduction_rate": 0.6,
        "min_context_tokens_reduction_rate": 0.6,
        "min_time_to_productive_action_reduction_rate": 0.4,
    }
    root = Path(output_root)
    documents = {
        "dev-087-observation.v1.json": observation,
        "dev-087-expectation.v1.json": expectation,
        "dev-087-thresholds.v1.json": thresholds,
    }
    for name, document in documents.items():
        _publish_no_clobber(
            root / "fixtures" / name,
            (canonical_json(document) + "\n").encode("utf-8"),
        )

    measurement = {
        "measurement_version": "paired-context-measurement@1",
        "method": "median of 9 warm local context-router calls",
        "time_scope": "context-ready latency, used as deterministic productive-action proxy",
        "token_method": "ceil(utf8 context bytes / 4)",
        "baseline_elapsed_ms": baseline_elapsed_ms,
        "candidate_elapsed_ms": candidate_elapsed_ms,
        "baseline_samples_ms": list(baseline_samples_ms),
        "candidate_samples_ms": list(candidate_samples_ms),
        "baseline_context_bytes": baseline["budget"]["included_bytes"],
        "candidate_context_bytes": candidate["budget"]["included_bytes"],
        "baseline_token_count": ceil(baseline["budget"]["included_bytes"] / 4),
        "candidate_token_count": ceil(candidate["budget"]["included_bytes"] / 4),
    }
    _publish_no_clobber(
        root / "fixtures" / "dev-087-measurement.v1.json",
        (canonical_json(measurement) + "\n").encode("utf-8"),
    )
    # Full contexts retain evidence locators, including local absolute paths.
    # Evaluate them from a private temporary directory and retain only hashes,
    # safe observations/expectations, measurements, and the derived report.
    with tempfile.TemporaryDirectory(prefix="shared-mind-dev087-context-") as raw:
        temporary = Path(raw)
        _publish_no_clobber(
            temporary / "baseline-context.json",
            (canonical_json(baseline) + "\n").encode("utf-8"),
        )
        _publish_no_clobber(
            temporary / "candidate-context.json",
            (canonical_json(candidate) + "\n").encode("utf-8"),
        )
        return run_paired_evaluation(
            temporary / "baseline-context.json",
            root / "fixtures" / "dev-087-observation.v1.json",
            temporary / "candidate-context.json",
            root / "fixtures" / "dev-087-observation.v1.json",
            root / "fixtures" / "dev-087-expectation.v1.json",
            root / "fixtures" / "dev-087-thresholds.v1.json",
            root / "results" / "dev-087-self-dogfood.v1.json",
            baseline_elapsed_ms=baseline_elapsed_ms,
            candidate_elapsed_ms=candidate_elapsed_ms,
            baseline_token_count=measurement["baseline_token_count"],
            candidate_token_count=measurement["candidate_token_count"],
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev-087-context-reduction-eval")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output-root", default="evals/shared_state_continuity")
    parser.add_argument("--baseline-elapsed-ms", type=float, required=True)
    parser.add_argument("--candidate-elapsed-ms", type=float, required=True)
    parser.add_argument("--baseline-sample-ms", type=float, action="append", required=True)
    parser.add_argument("--candidate-sample-ms", type=float, action="append", required=True)
    args = parser.parse_args(argv)
    result = build_evidence(
        args.workspace,
        args.output_root,
        baseline_elapsed_ms=args.baseline_elapsed_ms,
        candidate_elapsed_ms=args.candidate_elapsed_ms,
        baseline_samples_ms=args.baseline_sample_ms,
        candidate_samples_ms=args.candidate_sample_ms,
    )
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
