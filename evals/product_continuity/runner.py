"""Deterministic, offline scorer for product-continuity scenarios.

The scorer deliberately has no client or network integration.  A response is
compared with the context-grounded golden response embedded in the scenario,
and the scenario's recorded resource metrics are evaluated independently.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_DIMENSION_FIELDS = {
    "project_purpose": "project_purpose",
    "decision_and_rationale": "current_decisions",
    "settled_claim_and_evidence_locator": "settled_claims",
    "open_conflict_and_all_members": "open_conflicts",
    "open_question": "open_questions",
    "actionable_work": "actionable_work_items",
}


def evaluate_scenario(
    scenario: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a schema-shaped deterministic report for one offline response."""

    expected = _mapping(scenario["expected_response"], "expected_response")
    context = _mapping(scenario["context"], "context")
    scoring = _mapping(scenario["scoring"], "scoring")
    weights = _mapping(scoring["dimensions"], "scoring.dimensions")

    dimension_matches = {
        dimension: response.get(field) == expected.get(field)
        for dimension, field in _DIMENSION_FIELDS.items()
    }
    dimension_scores = {
        dimension: int(weights[dimension]) if dimension_matches[dimension] else 0
        for dimension in _DIMENSION_FIELDS
    }

    conflict_member_ids = _context_conflict_member_ids(context)
    settled_ids = _ids_in_records(response.get("settled_claims"), "claim_id")
    false_settled = bool(conflict_member_ids & settled_ids)

    grounded_ids = _object_ids(context)
    response_ids = _object_ids(response)
    hallucinated_ids = response_ids - grounded_ids

    conflict_recall, omitted_conflict_member = _open_conflict_member_recall(
        context, response
    )

    penalty_codes: list[str] = []
    if false_settled:
        penalty_codes.append("FALSE_SETTLED_CONFLICT_MEMBER")
    if hallucinated_ids:
        penalty_codes.append("HALLUCINATED_ID")
    if omitted_conflict_member:
        penalty_codes.append("OMITTED_CONFLICT_MEMBER")

    penalties = _mapping(scoring["penalties"], "scoring.penalties")
    earned_score = sum(dimension_scores.values())
    penalty_score = sum(int(penalties[code]) for code in penalty_codes)
    maximum_score = int(scoring["maximum_score"])
    score = max(0, min(maximum_score, earned_score - penalty_score))

    fact_accuracy = sum(dimension_matches.values()) / len(dimension_matches)
    fact_accuracy = round(fact_accuracy, 12)
    metric_comparison = _metric_comparison(scenario)
    passed = (
        score >= int(scoring["passing_score"])
        and fact_accuracy >= float(scoring["required_fact_accuracy"])
        and conflict_recall
        >= float(scoring["required_open_conflict_member_recall"])
        and metric_comparison["meets_reduction_target"]
        and metric_comparison["quality_preserved"]
        and not penalty_codes
    )

    return {
        "report_version": "product-continuity-report@1",
        "scenario_id": str(scenario["scenario_id"]),
        "score": score,
        "maximum_score": maximum_score,
        "passed": passed,
        "fact_accuracy": fact_accuracy,
        "open_conflict_member_recall": conflict_recall,
        "dimension_scores": dimension_scores,
        "penalty_codes": penalty_codes,
        "metric_comparison": metric_comparison,
    }


def _metric_comparison(scenario: Mapping[str, Any]) -> dict[str, Any]:
    metrics = _mapping(scenario["metrics"], "metrics")
    baseline = _mapping(metrics["manual_baseline"], "metrics.manual_baseline")
    context_only = _mapping(metrics["context_only"], "metrics.context_only")
    minimum = float(metrics["minimum_reduction_fraction"])

    reductions = {}
    for name in ("bytes", "tokens", "time_seconds"):
        raw_reduction = 1.0 - float(context_only[name]) / float(baseline[name])
        reductions[name] = round(max(0.0, min(1.0, raw_reduction)), 12)

    quality = _mapping(metrics["quality"], "metrics.quality")
    baseline_quality = _mapping(
        quality["manual_baseline"], "metrics.quality.manual_baseline"
    )
    context_quality = _mapping(
        quality["context_only"], "metrics.quality.context_only"
    )
    quality_preserved = all(
        float(context_quality[name]) >= float(value)
        for name, value in baseline_quality.items()
    )

    return {
        "reductions": reductions,
        "meets_reduction_target": all(
            reduction >= minimum for reduction in reductions.values()
        ),
        "quality_preserved": quality_preserved,
    }


def _open_conflict_member_recall(
    context: Mapping[str, Any], response: Mapping[str, Any]
) -> tuple[float, bool]:
    expected_members: set[tuple[str, str]] = set()
    for conflict in _records(context.get("open_conflicts")):
        conflict_id = conflict.get("conflict_id")
        if not isinstance(conflict_id, str):
            continue
        for member in _records(conflict.get("members")):
            claim_id = member.get("claim_id")
            if isinstance(claim_id, str):
                expected_members.add((conflict_id, claim_id))

    actual_members: set[tuple[str, str]] = set()
    for conflict in _records(response.get("open_conflicts")):
        conflict_id = conflict.get("conflict_id")
        if not isinstance(conflict_id, str):
            continue
        for member in _records(conflict.get("member_claims")):
            claim_id = member.get("claim_id")
            if isinstance(claim_id, str):
                actual_members.add((conflict_id, claim_id))

    if not expected_members:
        return 1.0, False
    recalled = len(expected_members & actual_members)
    recall = round(recalled / len(expected_members), 12)
    return recall, recalled != len(expected_members)


def _context_conflict_member_ids(context: Mapping[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    for conflict in _records(context.get("open_conflicts")):
        identifiers.update(_ids_in_records(conflict.get("members"), "claim_id"))
    return identifiers


def _ids_in_records(value: Any, key: str) -> set[str]:
    return {
        identifier
        for item in _records(value)
        if isinstance((identifier := item.get(key)), str)
    }


def _records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _object_ids(value: Any) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key != "scenario_id" and key.endswith("_id") and isinstance(item, str):
                identifiers.add(item)
            identifiers.update(_object_ids(item))
    elif isinstance(value, list):
        for item in value:
            identifiers.update(_object_ids(item))
    return identifiers


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return value
