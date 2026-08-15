"""Deterministic, offline scorer for product-continuity scenarios.

The scorer deliberately has no client or network integration.  A response is
compared with the context-grounded golden response embedded in the scenario,
and the scenario's recorded resource metrics are evaluated independently.
"""

from __future__ import annotations

import hashlib
import math
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
_SCORING_DIMENSIONS = {
    "project_purpose": 10,
    "decision_and_rationale": 20,
    "settled_claim_and_evidence_locator": 20,
    "open_conflict_and_all_members": 25,
    "open_question": 10,
    "actionable_work": 15,
}
_SCORING_PENALTIES = {
    "FALSE_SETTLED_CONFLICT_MEMBER": 50,
    "HALLUCINATED_ID": 25,
    "OMITTED_CONFLICT_MEMBER": 25,
}
_SCORING_CONSTANTS = {
    "maximum_score": 100,
    "passing_score": 100,
    "required_fact_accuracy": 1.0,
    "required_open_conflict_member_recall": 1.0,
}
_SCORING_FIELDS = frozenset(
    {
        *_SCORING_CONSTANTS,
        "dimensions",
        "penalties",
    }
)
_SCENARIO_FIELDS = frozenset(
    {
        "scenario_version",
        "scenario_id",
        "description",
        "response_schema",
        "metrics_schema",
        "report_schema",
        "execution_policy",
        "context",
        "expected_response",
        "adversarial_cases",
        "scoring",
        "metrics",
    }
)
_SCENARIO_PINS = {
    "scenario_version": "product-continuity-scenario@1",
    "response_schema": "product-continuity-response.schema.v1.json",
    "metrics_schema": "product-continuity-metrics.schema.v1.json",
    "report_schema": "product-continuity-report.schema.v2.json",
}
_CONTEXT_FIELDS = frozenset(
    {
        "context_pack_version",
        "evaluation_scenario_id",
        "projection_version",
        "ledger_seq",
        "state_root",
        "purpose",
        "purpose_missing",
        "current_claims",
        "open_conflicts",
        "decisions",
        "open_questions",
        "work_items",
        "truncation",
    }
)
_EXPECTED_RESPONSE_FIELDS = frozenset(
    {
        "scenario_id",
        "project_purpose",
        "current_decisions",
        "settled_claims",
        "open_conflicts",
        "open_questions",
        "actionable_work_items",
    }
)

LIVE_COMPARISON_V1 = "product-continuity-live-comparison@1"
LIVE_COMPARISON_V2 = "product-continuity-live-comparison@2"
_SUPPORTED_LIVE_COMPARISON_VERSIONS = frozenset(
    (LIVE_COMPARISON_V1, LIVE_COMPARISON_V2)
)
PRODUCT_CONTINUITY_REPORT_V1 = "product-continuity-report@1"
PRODUCT_CONTINUITY_REPORT_V2 = "product-continuity-report@2"
_SUPPORTED_PRODUCT_CONTINUITY_REPORT_VERSIONS = frozenset(
    (PRODUCT_CONTINUITY_REPORT_V1, PRODUCT_CONTINUITY_REPORT_V2)
)
PRODUCT_CONTINUITY_METRICS_V1 = "product-continuity-metrics@1"
_OFFLINE_METRIC_FIELDS = frozenset(
    {
        "metric_version",
        "minimum_reduction_fraction",
        "manual_baseline",
        "context_only",
        "quality",
    }
)
_RESOURCE_METRIC_FIELDS = frozenset({"bytes", "tokens", "time_seconds"})
_QUALITY_METRIC_FIELDS = frozenset(
    {"fact_accuracy", "open_conflict_member_recall"}
)


def evaluate_scenario(
    scenario: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    report_version: str = PRODUCT_CONTINUITY_REPORT_V2,
) -> dict[str, Any]:
    """Return a schema-shaped deterministic report for one offline response."""

    if report_version not in _SUPPORTED_PRODUCT_CONTINUITY_REPORT_VERSIONS:
        raise ValueError(
            "UNSUPPORTED_PRODUCT_CONTINUITY_REPORT_VERSION: "
            f"{report_version!r}"
        )

    expected, context = _scenario_contract(scenario)
    scoring = _scoring_contract(scenario.get("scoring"))
    weights = scoring["dimensions"]
    scenario_id_matches = response.get("scenario_id") == scenario.get("scenario_id")

    dimension_matches = {
        "project_purpose": response.get("project_purpose")
        == expected.get("project_purpose"),
        "decision_and_rationale": response.get("current_decisions")
        == expected.get("current_decisions"),
        "settled_claim_and_evidence_locator": _settled_claims_match(
            context, response
        ),
        "open_conflict_and_all_members": _open_conflicts_match(context, response),
        "open_question": response.get("open_questions")
        == expected.get("open_questions"),
        "actionable_work": response.get("actionable_work_items")
        == expected.get("actionable_work_items"),
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

    penalties = scoring["penalties"]
    earned_score = sum(dimension_scores.values())
    penalty_score = sum(int(penalties[code]) for code in penalty_codes)
    maximum_score = int(scoring["maximum_score"])
    score = (
        max(0, min(maximum_score, earned_score - penalty_score))
        if scenario_id_matches
        else 0
    )

    fact_accuracy = sum(dimension_matches.values()) / len(dimension_matches)
    fact_accuracy = round(fact_accuracy, 12)
    metric_comparison = _metric_comparison(
        scenario,
        report_version=report_version,
    )
    passed = (
        score >= int(scoring["passing_score"])
        and fact_accuracy >= float(scoring["required_fact_accuracy"])
        and conflict_recall
        >= float(scoring["required_open_conflict_member_recall"])
        and metric_comparison["meets_reduction_target"]
        and metric_comparison["quality_preserved"]
        and not penalty_codes
        and scenario_id_matches
    )

    return {
        "report_version": report_version,
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


def _scenario_contract(
    scenario: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if set(scenario) != _SCENARIO_FIELDS:
        _invalid_scenario("scenario must use the exact product-continuity field set")
    for field, expected_pin in _SCENARIO_PINS.items():
        if scenario.get(field) != expected_pin:
            _invalid_scenario(f"{field} must equal {expected_pin!r}")
    if not isinstance(scenario.get("description"), str) or not scenario[
        "description"
    ].strip():
        _invalid_scenario("description must be a non-empty string")
    scenario_id = scenario.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        _invalid_scenario("scenario_id must be a non-empty string")

    context = scenario.get("context")
    if not isinstance(context, Mapping) or set(context) != _CONTEXT_FIELDS:
        _invalid_scenario("context must use the exact scenario@1 field set")
    if context.get("evaluation_scenario_id") != scenario_id:
        _invalid_scenario("context evaluation_scenario_id must match scenario_id")
    if context.get("purpose_missing") is not False:
        _invalid_scenario("context purpose must be present")
    purpose = context.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        _invalid_scenario("context purpose must be a non-empty string")

    expected = scenario.get("expected_response")
    if not isinstance(expected, Mapping) or set(expected) != _EXPECTED_RESPONSE_FIELDS:
        _invalid_scenario("expected_response must use the exact response field set")
    if expected.get("scenario_id") != scenario_id:
        _invalid_scenario("expected_response scenario_id must match scenario_id")
    if expected.get("project_purpose") != purpose:
        _invalid_scenario("expected project purpose must match context purpose")

    for field in (
        "current_decisions",
        "settled_claims",
        "open_conflicts",
        "open_questions",
        "actionable_work_items",
    ):
        value = expected.get(field)
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, Mapping) for item in value)
        ):
            _invalid_scenario(f"expected_response.{field} must be non-empty records")

    if expected["current_decisions"] != _context_continuity_records(
        context.get("decisions"),
        fields=("decision_id", "title", "conclusion", "rationale"),
        path="context.decisions",
    ):
        _invalid_scenario("expected decisions must match context decisions")
    if not _settled_claims_match(context, expected):
        _invalid_scenario("expected settled claims must match context claims")
    if not _open_conflicts_match(context, expected):
        _invalid_scenario("expected open conflicts must match context conflicts")
    if expected["open_questions"] != _context_continuity_records(
        context.get("open_questions"),
        fields=("question_id", "question"),
        path="context.open_questions",
    ):
        _invalid_scenario("expected open questions must match context questions")
    if expected["actionable_work_items"] != _context_continuity_records(
        context.get("work_items"),
        fields=("work_item_id", "status", "description"),
        path="context.work_items",
    ):
        _invalid_scenario("expected work items must match context work items")
    return expected, context


def _context_continuity_records(
    value: Any,
    *,
    fields: tuple[str, ...],
    path: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _invalid_scenario(f"{path} must be non-empty records")
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(value):
        if not isinstance(record, Mapping):
            _invalid_scenario(f"{path}[{index}] must be a mapping")
        document = record.get("document")
        if not isinstance(document, Mapping) or any(
            field not in document for field in fields
        ):
            _invalid_scenario(f"{path}[{index}].document is incomplete")
        normalized.append({field: document[field] for field in fields})
    return normalized


def _invalid_scenario(message: str) -> None:
    raise ValueError(f"INVALID_SCENARIO_CONTRACT: {message}")


def _scoring_contract(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SCORING_FIELDS:
        raise ValueError(
            "INVALID_SCORING_CONTRACT: scoring must use the exact field set"
        )

    for field, expected in _SCORING_CONSTANTS.items():
        actual = value[field]
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError(
                f"INVALID_SCORING_CONTRACT: scoring.{field} must be the "
                f"exact typed constant {expected!r}"
            )

    _exact_integer_constants(
        value["dimensions"],
        expected=_SCORING_DIMENSIONS,
        path="scoring.dimensions",
    )
    _exact_integer_constants(
        value["penalties"],
        expected=_SCORING_PENALTIES,
        path="scoring.penalties",
    )
    return value


def _exact_integer_constants(
    value: Any,
    *,
    expected: Mapping[str, int],
    path: str,
) -> None:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise ValueError(
            f"INVALID_SCORING_CONTRACT: {path} must use the exact field set"
        )
    for field, expected_value in expected.items():
        actual = value[field]
        if type(actual) is not int or actual != expected_value:
            raise ValueError(
                f"INVALID_SCORING_CONTRACT: {path}.{field} must be the "
                f"exact integer constant {expected_value}"
            )


def sha256_bytes(value: bytes) -> str:
    """Return the canonical hash string used by live summary artifacts."""

    return "sha256:" + hashlib.sha256(value).hexdigest()


def live_summary_comparison(
    summary: Mapping[str, Any],
    *,
    comparison_version: str = LIVE_COMPARISON_V2,
) -> dict[str, Any]:
    """Compare sanitized live arms with explicitly versioned reduction semantics.

    Version 1 retains its historical zero-to-one clamp so checked-in evidence
    remains exactly reproducible.  Version 2 preserves negative reductions,
    making a context arm that is slower or more expensive visible as a
    regression instead of reporting it as zero improvement.
    """

    if comparison_version not in _SUPPORTED_LIVE_COMPARISON_VERSIONS:
        raise ValueError(
            "UNSUPPORTED_LIVE_COMPARISON_VERSION: "
            f"{comparison_version!r}"
        )

    arms = _mapping(summary["arms"], "arms")
    manual = _mapping(arms["manual_baseline"], "arms.manual_baseline")
    context = _mapping(arms["context_only"], "arms.context_only")

    reductions = {}
    for output_name, input_name in (
        ("bytes", "input_bytes"),
        ("tokens", "input_tokens"),
        ("time_seconds", "elapsed_time_seconds"),
    ):
        baseline = _positive_live_metric(
            manual[input_name], f"arms.manual_baseline.{input_name}"
        )
        candidate = _positive_live_metric(
            context[input_name], f"arms.context_only.{input_name}"
        )
        reduction = 1.0 - candidate / baseline
        if comparison_version == LIVE_COMPARISON_V1:
            reduction = max(0.0, min(1.0, reduction))
        reductions[output_name] = round(reduction, 12)

    manual_report = _mapping(manual["report"], "arms.manual_baseline.report")
    context_report = _mapping(context["report"], "arms.context_only.report")
    manual_quality = _live_report_quality(
        manual_report, "arms.manual_baseline.report"
    )
    context_quality = _live_report_quality(
        context_report, "arms.context_only.report"
    )
    quality_preserved = (
        context_quality["score"] >= manual_quality["score"]
        and context_quality["fact_accuracy"] >= manual_quality["fact_accuracy"]
        and context_quality["open_conflict_member_recall"]
        >= manual_quality["open_conflict_member_recall"]
    )
    schema_valid = (
        manual.get("schema_validation") == "PASS"
        and context.get("schema_validation") == "PASS"
    )
    meets_reduction_target = all(
        reduction >= 0.5 for reduction in reductions.values()
    )
    passed = (
        manual_quality["passed"]
        and context_quality["passed"]
        and schema_valid
        and quality_preserved
        and meets_reduction_target
    )

    return {
        "report_version": comparison_version,
        "reductions": reductions,
        "meets_reduction_target": meets_reduction_target,
        "quality_preserved": quality_preserved,
        "schema_valid": schema_valid,
        "passed": passed,
    }


def _positive_live_metric(value: Any, path: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(
            f"INVALID_LIVE_COMPARISON_METRIC: {path} must be finite and positive"
        )
    return float(value)


def _live_report_quality(
    report: Mapping[str, Any], path: str
) -> dict[str, int | float | bool]:
    version = report.get("report_version")
    if version not in _SUPPORTED_PRODUCT_CONTINUITY_REPORT_VERSIONS:
        raise ValueError(
            f"INVALID_LIVE_REPORT_VERSION: {path}.report_version must be a "
            "supported product-continuity report version"
        )
    passed = report.get("passed")
    if not isinstance(passed, bool):
        raise ValueError(
            f"INVALID_LIVE_REPORT_PASSED: {path}.passed must be boolean"
        )
    score = report.get("score")
    if (
        isinstance(score, bool)
        or not isinstance(score, int)
        or not 0 <= score <= 100
    ):
        raise ValueError(
            f"INVALID_LIVE_REPORT_QUALITY: {path}.score must be an integer "
            "between zero and 100"
        )
    return {
        "passed": passed,
        "score": score,
        "fact_accuracy": _quality_fraction(
            report.get("fact_accuracy"),
            f"{path}.fact_accuracy",
            "INVALID_LIVE_REPORT_QUALITY",
        ),
        "open_conflict_member_recall": _quality_fraction(
            report.get("open_conflict_member_recall"),
            f"{path}.open_conflict_member_recall",
            "INVALID_LIVE_REPORT_QUALITY",
        ),
    }


def _metric_comparison(
    scenario: Mapping[str, Any],
    *,
    report_version: str,
) -> dict[str, Any]:
    metrics = _mapping(scenario["metrics"], "metrics")
    if set(metrics) != _OFFLINE_METRIC_FIELDS:
        raise ValueError(
            "INVALID_OFFLINE_METRICS_SHAPE: metrics must use the exact "
            "product-continuity-metrics@1 field set"
        )
    if metrics.get("metric_version") != PRODUCT_CONTINUITY_METRICS_V1:
        raise ValueError(
            "INVALID_OFFLINE_METRIC_VERSION: metrics.metric_version must be "
            "product-continuity-metrics@1"
        )
    baseline = _mapping(metrics["manual_baseline"], "metrics.manual_baseline")
    context_only = _mapping(metrics["context_only"], "metrics.context_only")
    if set(baseline) != _RESOURCE_METRIC_FIELDS or set(context_only) != (
        _RESOURCE_METRIC_FIELDS
    ):
        raise ValueError(
            "INVALID_OFFLINE_METRICS_SHAPE: resource metrics must contain "
            "exactly bytes, tokens, and time_seconds"
        )
    minimum = _offline_reduction_threshold(
        metrics["minimum_reduction_fraction"]
    )

    reductions = {}
    for name in ("bytes", "tokens", "time_seconds"):
        baseline_value = _positive_offline_metric(
            baseline[name], f"metrics.manual_baseline.{name}"
        )
        context_value = _positive_offline_metric(
            context_only[name], f"metrics.context_only.{name}"
        )
        reduction = 1.0 - context_value / baseline_value
        if report_version == PRODUCT_CONTINUITY_REPORT_V1:
            reduction = max(0.0, min(1.0, reduction))
        reductions[name] = round(reduction, 12)

    quality = _mapping(metrics["quality"], "metrics.quality")
    if set(quality) != {"manual_baseline", "context_only"}:
        raise ValueError(
            "INVALID_OFFLINE_QUALITY_METRICS: metrics.quality must contain "
            "exactly manual_baseline and context_only"
        )
    baseline_quality = _mapping(
        quality["manual_baseline"], "metrics.quality.manual_baseline"
    )
    context_quality = _mapping(
        quality["context_only"], "metrics.quality.context_only"
    )
    baseline_values = _offline_quality_metrics(
        baseline_quality, "metrics.quality.manual_baseline"
    )
    context_values = _offline_quality_metrics(
        context_quality, "metrics.quality.context_only"
    )
    quality_preserved = all(
        context_values[name] >= value for name, value in baseline_values.items()
    )

    return {
        "reductions": reductions,
        "meets_reduction_target": all(
            reduction >= minimum for reduction in reductions.values()
        ),
        "quality_preserved": quality_preserved,
    }


def _positive_offline_metric(value: Any, path: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(
            f"INVALID_OFFLINE_COMPARISON_METRIC: {path} must be finite and positive"
        )
    return float(value)


def _offline_reduction_threshold(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) != 0.5
    ):
        raise ValueError(
            "INVALID_OFFLINE_REDUCTION_THRESHOLD: "
            "metrics.minimum_reduction_fraction must be 0.5"
        )
    return float(value)


def _offline_quality_metrics(
    value: Mapping[str, Any], path: str
) -> dict[str, float]:
    if set(value) != _QUALITY_METRIC_FIELDS:
        raise ValueError(
            f"INVALID_OFFLINE_QUALITY_METRICS: {path} must contain exactly "
            "fact_accuracy and open_conflict_member_recall"
        )
    return {
        name: _quality_fraction(
            value[name], f"{path}.{name}", "INVALID_OFFLINE_QUALITY_METRICS"
        )
        for name in sorted(_QUALITY_METRIC_FIELDS)
    }


def _quality_fraction(value: Any, path: str, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{code}: {path} must be a finite number from zero to one")
    return float(value)


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


def _settled_claims_match(
    context: Mapping[str, Any], response: Mapping[str, Any]
) -> bool:
    expected = {
        claim["claim_id"]: (
            claim.get("proposition"),
            claim.get("proposition_hash"),
            _evidence_locator_tuples(claim.get("evidence")),
        )
        for claim in _records(context.get("current_claims"))
        if isinstance(claim.get("claim_id"), str)
    }
    actual_records = _records(response.get("settled_claims"))
    actual = {
        claim.get("claim_id"): (
            claim.get("proposition"),
            claim.get("proposition_hash"),
            _evidence_locator_tuples(claim.get("evidence_locators")),
        )
        for claim in actual_records
        if isinstance(claim.get("claim_id"), str)
    }
    return (
        actual == expected
        and len(actual_records) == len(expected)
        and all(_has_grounded_summary(claim) for claim in actual_records)
    )


def _open_conflicts_match(
    context: Mapping[str, Any], response: Mapping[str, Any]
) -> bool:
    expected = {
        conflict["conflict_id"]: (
            conflict.get("status"),
            {
                member["claim_id"]: (
                    member.get("proposition"),
                    member.get("proposition_hash"),
                    member.get("status"),
                )
                for member in _records(conflict.get("members"))
                if isinstance(member.get("claim_id"), str)
            },
        )
        for conflict in _records(context.get("open_conflicts"))
        if isinstance(conflict.get("conflict_id"), str)
    }
    actual_records = _records(response.get("open_conflicts"))
    actual = {}
    for conflict in actual_records:
        conflict_id = conflict.get("conflict_id")
        if not isinstance(conflict_id, str):
            continue
        members = _records(conflict.get("member_claims"))
        if not all(_has_grounded_summary(member) for member in members):
            return False
        member_ids = [
            member.get("claim_id")
            for member in members
            if isinstance(member.get("claim_id"), str)
        ]
        if len(member_ids) != len(members) or len(set(member_ids)) != len(member_ids):
            return False
        actual[conflict_id] = (
            conflict.get("status"),
            {
                member["claim_id"]: (
                    member.get("proposition"),
                    member.get("proposition_hash"),
                    member.get("status"),
                )
                for member in members
                if isinstance(member.get("claim_id"), str)
            },
        )
    return actual == expected and len(actual_records) == len(expected)


def _evidence_locator_tuples(value: Any) -> set[tuple[str, str, int, int, str]]:
    locators: set[tuple[str, str, int, int, str]] = set()
    for locator in _records(value):
        evidence_link_id = locator.get("evidence_link_id")
        source_revision_id = locator.get("source_revision_id")
        selector = (
            _mapping(locator.get("selector"), "selector")
            if isinstance(locator.get("selector"), Mapping)
            else locator
        )
        start_byte = selector.get("start_byte")
        end_byte = selector.get("end_byte")
        excerpt_hash = selector.get("excerpt_hash")
        if (
            isinstance(evidence_link_id, str)
            and isinstance(source_revision_id, str)
            and isinstance(start_byte, int)
            and isinstance(end_byte, int)
            and isinstance(excerpt_hash, str)
        ):
            locators.add(
                (
                    evidence_link_id,
                    source_revision_id,
                    start_byte,
                    end_byte,
                    excerpt_hash,
                )
            )
    return locators


def _has_grounded_summary(record: Mapping[str, Any]) -> bool:
    summary = record.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False
    identifier = record.get("claim_id")
    return isinstance(identifier, str) and summary.strip() != identifier


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
