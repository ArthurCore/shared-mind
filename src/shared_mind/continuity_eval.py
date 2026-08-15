"""Deterministic evaluation of continuity over one Shared State.

The reports produced here are evidence, not canonical memory.  Every function
is read-only and requires its caller to supply the context/state snapshots that
are being evaluated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import sha256_json


class ContinuityEvaluationError(ValueError):
    """A malformed or unsupported evaluation input."""

    def __init__(self, code: str, message: str, *, path: str = "$") -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)


_CURRENT_STATUSES = frozenset(
    {
        "ACTIVE",
        "OPEN",
        "REOPENED",
        "TODO",
        "DOING",
        "BLOCKED",
        "ASSERTED",
        "READY",
        "IMMUTABLE",
        "TESTED",
        "APPROVED",
    }
)
_STALE_STATUSES = frozenset({"STALE", "EXPIRED"})
_SUPERSEDED_STATUSES = frozenset(
    {"SUPERSEDED", "REVERSED", "RETRACTED", "DEPRECATED"}
)
_COMPLETED_STATUSES = frozenset(
    {"DONE", "ANSWERED", "RESOLVED", "DROPPED", "COMMITTED", "REJECTED"}
)


def classify_memory_lifecycle(record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a canonical/product status without discarding history."""

    item = _mapping(record, "record", "LIFECYCLE_INPUT_INVALID")
    status = item.get("status")
    if not isinstance(status, str) or not status:
        raise ContinuityEvaluationError(
            "LIFECYCLE_STATUS_INVALID", "record.status must be a non-empty string", path="$.status"
        )
    if status in _CURRENT_STATUSES:
        lifecycle = "CURRENT"
    elif status in _STALE_STATUSES:
        lifecycle = "STALE"
    elif status in _SUPERSEDED_STATUSES:
        lifecycle = "SUPERSEDED"
    elif status in _COMPLETED_STATUSES:
        lifecycle = "COMPLETED"
    else:
        raise ContinuityEvaluationError(
            "LIFECYCLE_STATUS_UNSUPPORTED",
            f"Unsupported lifecycle status: {status}",
            path="$.status",
        )
    return {
        "lifecycle_version": "memory-lifecycle@1",
        "object_id": _object_id(item),
        "status": status,
        "lifecycle": lifecycle,
        "eligible_for_current_context": lifecycle == "CURRENT",
        "preserve_history": True,
    }


def evaluate_zero_relearning(
    context: Mapping[str, Any],
    observation: Mapping[str, Any],
    expectation: Mapping[str, Any],
    *,
    elapsed_ms: int | float,
    token_count: int | None = None,
) -> dict[str, Any]:
    """Score a fresh-session observation against explicit canonical expectations."""

    context_document = _context(context)
    actual = _versioned_mapping(
        observation,
        "observation_version",
        "zero-relearning-observation@1",
        "OBSERVATION_INVALID",
    )
    expected = _versioned_mapping(
        expectation,
        "expectation_version",
        "zero-relearning-expectation@1",
        "EXPECTATION_INVALID",
    )
    elapsed = _non_negative_number(elapsed_ms, "$.elapsed_ms")
    tokens = _optional_non_negative_integer(token_count, "$.token_count")

    expected_decisions = _id_set(expected, "decision_ids", "EXPECTATION_INVALID")
    expected_questions = _id_set(expected, "open_question_ids", "EXPECTATION_INVALID")
    expected_conflicts = _id_set(expected, "conflict_ids", "EXPECTATION_INVALID")
    expected_sources = _id_set(
        expected, "evidence_source_revision_ids", "EXPECTATION_INVALID"
    )
    actual_decisions = _id_set(actual, "decision_ids", "OBSERVATION_INVALID")
    actual_questions = _id_set(actual, "open_question_ids", "OBSERVATION_INVALID")
    actual_conflicts = _id_set(actual, "conflict_ids", "OBSERVATION_INVALID")
    actual_sources = _id_set(
        actual, "evidence_source_revision_ids", "OBSERVATION_INVALID"
    )
    expected_work = _non_empty_string(
        expected.get("active_work_item_id"), "$.active_work_item_id", "EXPECTATION_INVALID"
    )
    actual_work = _non_empty_string(
        actual.get("active_work_item_id"), "$.active_work_item_id", "OBSERVATION_INVALID"
    )
    expected_purpose = _non_empty_string(
        expected.get("purpose"), "$.purpose", "EXPECTATION_INVALID"
    )
    actual_purpose = _non_empty_string(
        actual.get("purpose"), "$.purpose", "OBSERVATION_INVALID"
    )

    memory_truth = _string_mapping(
        expected.get("memory_truth"), "$.memory_truth", "EXPECTATION_INVALID"
    )
    assertions = _assertions(actual.get("memory_assertions"))
    assertion_by_id = {item["memory_id"]: item for item in assertions}
    wrong_memory_ids = sorted(
        memory_id
        for memory_id, item in assertion_by_id.items()
        if memory_id not in memory_truth or item["value"] != memory_truth[memory_id]
    )
    missing_truth_ids = sorted(set(memory_truth) - set(assertion_by_id))
    semantic_accuracy = (
        sum(
            assertion_by_id.get(memory_id, {}).get("value") == value
            for memory_id, value in memory_truth.items()
        )
        / len(memory_truth)
        if memory_truth
        else 1.0
    )

    decision_recall = _recall(actual_decisions, expected_decisions)
    question_recall = _recall(actual_questions, expected_questions)
    conflict_recall = _recall(actual_conflicts, expected_conflicts)
    evidence_traceability = _recall(actual_sources, expected_sources)
    purpose_accuracy = float(actual_purpose == expected_purpose)
    active_work_accuracy = float(actual_work == expected_work)
    continuity_accuracy = sum(
        (
            purpose_accuracy,
            decision_recall,
            question_recall,
            conflict_recall,
            active_work_accuracy,
            evidence_traceability,
            semantic_accuracy,
        )
    ) / 7

    observed_canonical_ids = {
        *actual_decisions,
        *actual_questions,
        *actual_conflicts,
        actual_work,
        *actual_sources,
    }
    critical_ids = _id_set(expected, "critical_memory_ids", "EXPECTATION_INVALID")
    missing_canonical_ids = critical_ids - observed_canonical_ids
    missing_critical_ids = sorted(
        [*missing_canonical_ids, *(f"truth:{item}" for item in missing_truth_ids)]
    )
    critical_count = len(critical_ids) + len(memory_truth)
    missing_critical_rate = (
        len(missing_critical_ids) / critical_count if critical_count else 0.0
    )

    included_ids = {
        str(item["id"])
        for item in context_document["selection_trace"]
        if item.get("included") is True and isinstance(item.get("id"), str)
    }
    relevant_ids = _optional_id_set(expected, "relevant_context_ids") or critical_ids
    irrelevant_ids = sorted(included_ids - relevant_ids)
    irrelevant_rate = len(irrelevant_ids) / len(included_ids) if included_ids else 0.0
    context_bytes = _non_negative_integer(
        context_document["budget"].get("included_bytes"), "$.context.budget.included_bytes"
    )
    metrics = {
        "continuity_accuracy": continuity_accuracy,
        "decision_recall": decision_recall,
        "open_question_recall": question_recall,
        "conflict_recall": conflict_recall,
        "evidence_traceability": evidence_traceability,
        "wrong_memory_rate": len(wrong_memory_ids) / len(assertions) if assertions else 0.0,
        "missing_critical_memory_rate": missing_critical_rate,
        "irrelevant_context_rate": irrelevant_rate,
        "context_bytes": context_bytes,
        "context_tokens": tokens,
        "time_to_productive_action_ms": elapsed,
    }
    thresholds = _thresholds(expected.get("thresholds"))
    failures = _metric_failures(metrics, thresholds)
    return {
        "report_version": "zero-relearning-eval@1",
        "evaluator_version": "shared-state-continuity-evaluator@1",
        "context_hash": context_document["context_hash"],
        "kernel_state_root": context_document["kernel_state_root"],
        "expectation_hash": sha256_json(expected),
        "observation_hash": sha256_json(actual),
        "metrics": metrics,
        "missing_critical_ids": missing_critical_ids,
        "wrong_memory_ids": wrong_memory_ids,
        "irrelevant_context_ids": irrelevant_ids,
        "failures": failures,
        "passed": not failures,
    }


def evaluate_memory_pollution(
    memories: Sequence[Mapping[str, Any]],
    *,
    expected_truth: Mapping[str, str],
    confident_threshold: float = 0.9,
) -> dict[str, Any]:
    """Measure duplicate, irrelevant, stale, and wrong selected memories."""

    if isinstance(memories, (str, bytes)) or not isinstance(memories, Sequence) or not memories:
        raise ContinuityEvaluationError(
            "POLLUTION_INPUT_EMPTY", "memories must contain at least one item", path="$.memories"
        )
    truth = _string_mapping(expected_truth, "$.expected_truth", "POLLUTION_INPUT_INVALID")
    threshold = _fraction(confident_threshold, "$.confident_threshold")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(memories):
        item = _mapping(raw, f"memories[{index}]", "POLLUTION_INPUT_INVALID")
        memory_id = _non_empty_string(
            item.get("memory_id"), f"$.memories[{index}].memory_id", "POLLUTION_INPUT_INVALID"
        )
        if memory_id in ids:
            raise ContinuityEvaluationError(
                "POLLUTION_MEMORY_ID_DUPLICATE",
                f"duplicate memory_id: {memory_id}",
                path=f"$.memories[{index}].memory_id",
            )
        ids.add(memory_id)
        lifecycle = _non_empty_string(
            item.get("lifecycle"), f"$.memories[{index}].lifecycle", "POLLUTION_INPUT_INVALID"
        )
        if lifecycle not in {"CURRENT", "STALE", "SUPERSEDED", "COMPLETED"}:
            raise ContinuityEvaluationError(
                "POLLUTION_LIFECYCLE_INVALID", lifecycle, path=f"$.memories[{index}].lifecycle"
            )
        relevant = item.get("relevant")
        if not isinstance(relevant, bool):
            raise ContinuityEvaluationError(
                "POLLUTION_RELEVANCE_INVALID", "relevant must be boolean", path=f"$.memories[{index}].relevant"
            )
        confidence = _fraction(item.get("confidence"), f"$.memories[{index}].confidence")
        normalized.append(
            {
                "memory_id": memory_id,
                "semantic_key": _non_empty_string(
                    item.get("semantic_key"), f"$.memories[{index}].semantic_key", "POLLUTION_INPUT_INVALID"
                ),
                "value": _non_empty_string(
                    item.get("value"), f"$.memories[{index}].value", "POLLUTION_INPUT_INVALID"
                ),
                "lifecycle": lifecycle,
                "relevant": relevant,
                "confidence": confidence,
            }
        )

    seen_keys: set[str] = set()
    duplicate_ids: list[str] = []
    for item in normalized:
        if item["semantic_key"] in seen_keys:
            duplicate_ids.append(item["memory_id"])
        else:
            seen_keys.add(item["semantic_key"])
    irrelevant_ids = sorted(item["memory_id"] for item in normalized if not item["relevant"])
    stale_ids = sorted(item["memory_id"] for item in normalized if item["lifecycle"] == "STALE")
    wrong_ids = sorted(
        item["memory_id"]
        for item in normalized
        if item["semantic_key"] in truth and item["value"] != truth[item["semantic_key"]]
    )
    confidently_wrong_ids = sorted(
        item["memory_id"]
        for item in normalized
        if item["memory_id"] in wrong_ids and item["confidence"] >= threshold
    )
    total = len(normalized)
    metrics = {
        "duplicate_memory_rate": len(duplicate_ids) / total,
        "irrelevant_memory_rate": len(irrelevant_ids) / total,
        "stale_memory_rate": len(stale_ids) / total,
        "wrong_memory_rate": len(wrong_ids) / total,
        "confidently_wrong_memory_rate": len(confidently_wrong_ids) / total,
    }
    failures = []
    for name, values in (
        ("DUPLICATE_MEMORY", duplicate_ids),
        ("IRRELEVANT_MEMORY", irrelevant_ids),
        ("STALE_MEMORY", stale_ids),
        ("WRONG_MEMORY", wrong_ids),
        ("CONFIDENTLY_WRONG_MEMORY", confidently_wrong_ids),
    ):
        if values:
            failures.append(name)
    return {
        "report_version": "memory-pollution-eval@1",
        "input_hash": sha256_json(normalized),
        "metrics": metrics,
        "duplicate_memory_ids": duplicate_ids,
        "irrelevant_memory_ids": irrelevant_ids,
        "stale_memory_ids": stale_ids,
        "wrong_memory_ids": wrong_ids,
        "confidently_wrong_memory_ids": confidently_wrong_ids,
        "failures": failures,
        "passed": not failures,
    }


def evaluate_conflict_resolution(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify that explicit resolution preserves the original conflict evidence."""

    original = _mapping(before, "before", "CONFLICT_EVALUATION_INVALID")
    resolved = _mapping(after, "after", "CONFLICT_EVALUATION_INVALID")
    failures: list[str] = []
    for field, reason in (
        ("conflict_id", "CONFLICT_ID_CHANGED"),
        ("episode", "CONFLICT_EPISODE_CHANGED"),
        ("member_digest", "CONFLICT_MEMBER_DIGEST_CHANGED"),
        ("members", "CONFLICT_MEMBERS_CHANGED"),
    ):
        if original.get(field) != resolved.get(field):
            failures.append(reason)
    if original.get("status") != "OPEN" or resolved.get("status") != "RESOLVED":
        failures.append("CONFLICT_STATUS_TRANSITION_INVALID")
    if not isinstance(original.get("version"), int) or resolved.get("version") != original["version"] + 1:
        failures.append("CONFLICT_VERSION_TRANSITION_INVALID")

    members = _string_sequence(original.get("members"), "$.before.members", "CONFLICT_EVALUATION_INVALID")
    before_claims = _mapping(original.get("claims"), "before.claims", "CONFLICT_EVALUATION_INVALID")
    after_claims = _mapping(resolved.get("claims"), "after.claims", "CONFLICT_EVALUATION_INVALID")
    preserved = [member for member in members if member in after_claims and after_claims[member] == before_claims.get(member)]
    if len(preserved) != len(members):
        failures.append("ORIGINAL_CLAIM_MISSING")

    resolution = resolved.get("resolution")
    if not isinstance(resolution, Mapping):
        failures.append("RESOLUTION_MISSING")
        resolution = {}
    selected = set(_soft_string_sequence(resolution.get("selected_claim_ids")))
    rejected = set(_soft_string_sequence(resolution.get("rejected_claim_ids")))
    partition_complete = not (selected & rejected) and selected | rejected == set(members)
    if not partition_complete:
        failures.append("MEMBER_PARTITION_INCOMPLETE")
    rationale_preserved = isinstance(resolution.get("rationale"), str) and bool(
        resolution.get("rationale", "").strip()
    )
    if not rationale_preserved:
        failures.append("RESOLUTION_RATIONALE_MISSING")
    if resolution.get("resolution_epoch") != original.get("episode"):
        failures.append("RESOLUTION_EPOCH_MISMATCH")
    if not isinstance(resolution.get("resolver"), Mapping):
        failures.append("RESOLUTION_RESOLVER_MISSING")
    if not _soft_string_sequence(resolution.get("evidence_link_ids")):
        failures.append("RESOLUTION_EVIDENCE_MISSING")
    if not isinstance(resolution.get("decided_at"), str) or not resolution.get("decided_at"):
        failures.append("RESOLUTION_TIME_MISSING")
    failures = list(dict.fromkeys(failures))
    return {
        "report_version": "conflict-resolution-eval@1",
        "before_hash": sha256_json(original),
        "after_hash": sha256_json(resolved),
        "metrics": {
            "original_claim_preservation": len(preserved) / len(members) if members else 1.0,
            "member_partition_complete": partition_complete,
            "rationale_preserved": rationale_preserved,
            "evidence_link_count": len(_soft_string_sequence(resolution.get("evidence_link_ids"))),
        },
        "failures": failures,
        "passed": not failures,
    }


def benchmark_context_quality(
    context: Mapping[str, Any],
    observation: Mapping[str, Any],
    expectation: Mapping[str, Any],
    *,
    elapsed_ms: int | float,
    token_count: int | None = None,
) -> dict[str, Any]:
    """Produce the DEV-086 benchmark from the same measured continuity run."""

    zero = evaluate_zero_relearning(
        context,
        observation,
        expectation,
        elapsed_ms=elapsed_ms,
        token_count=token_count,
    )
    expected = _mapping(expectation, "expectation", "EXPECTATION_INVALID")
    context_document = _context(context)
    relevant = _optional_id_set(expected, "relevant_context_ids") or _id_set(
        expected, "critical_memory_ids", "EXPECTATION_INVALID"
    )
    included = {
        str(item["id"])
        for item in context_document["selection_trace"]
        if item.get("included") is True and isinstance(item.get("id"), str)
    }
    metrics = {
        "relevant_recall": _recall(included, relevant),
        "missing_critical_memory_rate": zero["metrics"]["missing_critical_memory_rate"],
        "irrelevant_context_rate": zero["metrics"]["irrelevant_context_rate"],
        "evidence_traceability": zero["metrics"]["evidence_traceability"],
        "context_bytes": zero["metrics"]["context_bytes"],
        "context_tokens": zero["metrics"]["context_tokens"],
        "time_to_productive_action_ms": zero["metrics"]["time_to_productive_action_ms"],
    }
    failures = list(zero["failures"])
    if metrics["relevant_recall"] < 1.0:
        failures.append("RELEVANT_RECALL_BELOW_THRESHOLD")
    failures = list(dict.fromkeys(failures))
    return {
        "report_version": "context-quality-benchmark@1",
        "evaluator_version": zero["evaluator_version"],
        "context_hash": zero["context_hash"],
        "kernel_state_root": zero["kernel_state_root"],
        "continuity_report_hash": sha256_json(zero),
        "metrics": metrics,
        "failures": failures,
        "passed": not failures,
    }


def _context(value: Mapping[str, Any]) -> dict[str, Any]:
    document = _mapping(value, "context", "CONTEXT_INVALID")
    for field in ("context_hash", "kernel_state_root"):
        _non_empty_string(document.get(field), f"$.{field}", "CONTEXT_INVALID")
    trace = document.get("selection_trace")
    if isinstance(trace, (str, bytes)) or not isinstance(trace, Sequence):
        raise ContinuityEvaluationError(
            "CONTEXT_INVALID", "selection_trace must be an array", path="$.selection_trace"
        )
    budget = document.get("budget")
    if not isinstance(budget, Mapping):
        raise ContinuityEvaluationError(
            "CONTEXT_INVALID", "budget must be an object", path="$.budget"
        )
    return dict(document)


def _versioned_mapping(
    value: Mapping[str, Any], field: str, expected: str, code: str
) -> dict[str, Any]:
    document = _mapping(value, field, code)
    if document.get(field) != expected:
        raise ContinuityEvaluationError(code, f"{field} must be {expected}", path=f"$.{field}")
    return dict(document)


def _mapping(value: Any, name: str, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContinuityEvaluationError(code, f"{name} must be an object")
    return value


def _string_mapping(value: Any, path: str, code: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ContinuityEvaluationError(code, f"{path} must be an object", path=path)
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, str) or not item:
            raise ContinuityEvaluationError(code, f"{path} must map non-empty strings", path=path)
        result[key] = item
    return result


def _assertions(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContinuityEvaluationError(
            "OBSERVATION_INVALID", "memory_assertions must be an array", path="$.memory_assertions"
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _mapping(raw, f"memory_assertions[{index}]", "OBSERVATION_INVALID")
        memory_id = _non_empty_string(
            item.get("memory_id"), f"$.memory_assertions[{index}].memory_id", "OBSERVATION_INVALID"
        )
        if memory_id in seen:
            raise ContinuityEvaluationError(
                "OBSERVATION_ASSERTION_DUPLICATE", memory_id, path=f"$.memory_assertions[{index}].memory_id"
            )
        seen.add(memory_id)
        result.append(
            {
                "memory_id": memory_id,
                "value": _non_empty_string(
                    item.get("value"), f"$.memory_assertions[{index}].value", "OBSERVATION_INVALID"
                ),
                "confidence": _fraction(
                    item.get("confidence"), f"$.memory_assertions[{index}].confidence"
                ),
            }
        )
    return result


def _thresholds(value: Any) -> dict[str, int | float]:
    document = _mapping(value, "thresholds", "EXPECTATION_INVALID")
    required = {
        "continuity_accuracy",
        "decision_recall",
        "open_question_recall",
        "conflict_recall",
        "evidence_traceability",
        "wrong_memory_rate",
        "missing_critical_memory_rate",
        "irrelevant_context_rate",
        "max_context_bytes",
        "max_context_tokens",
        "max_time_to_productive_action_ms",
    }
    missing = sorted(required - set(document))
    if missing:
        raise ContinuityEvaluationError(
            "EXPECTATION_INVALID", "missing thresholds: " + ",".join(missing), path="$.thresholds"
        )
    result: dict[str, int | float] = {}
    for name in required:
        if name.startswith("max_"):
            result[name] = _non_negative_number(document[name], f"$.thresholds.{name}")
        else:
            result[name] = _fraction(document[name], f"$.thresholds.{name}")
    return result


def _metric_failures(
    metrics: Mapping[str, int | float | None], thresholds: Mapping[str, int | float]
) -> list[str]:
    failures: list[str] = []
    minimum_metrics = (
        "continuity_accuracy",
        "decision_recall",
        "open_question_recall",
        "conflict_recall",
        "evidence_traceability",
    )
    maximum_metrics = (
        "wrong_memory_rate",
        "missing_critical_memory_rate",
        "irrelevant_context_rate",
    )
    for name in minimum_metrics:
        if float(metrics[name]) < float(thresholds[name]):
            failures.append(name.upper() + "_BELOW_THRESHOLD")
    for name in maximum_metrics:
        if float(metrics[name]) > float(thresholds[name]):
            failures.append(name.upper() + "_ABOVE_THRESHOLD")
    for metric, threshold in (
        ("context_bytes", "max_context_bytes"),
        ("context_tokens", "max_context_tokens"),
        ("time_to_productive_action_ms", "max_time_to_productive_action_ms"),
    ):
        value = metrics[metric]
        if value is None or float(value) > float(thresholds[threshold]):
            failures.append(metric.upper() + "_ABOVE_THRESHOLD")
    return failures


def _id_set(document: Mapping[str, Any], field: str, code: str) -> set[str]:
    return set(_string_sequence(document.get(field), f"$.{field}", code))


def _optional_id_set(document: Mapping[str, Any], field: str) -> set[str]:
    if field not in document:
        return set()
    return set(_string_sequence(document[field], f"$.{field}", "EXPECTATION_INVALID"))


def _string_sequence(value: Any, path: str, code: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContinuityEvaluationError(code, f"{path} must be an array", path=path)
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ContinuityEvaluationError(code, "array items must be non-empty strings", path=f"{path}[{index}]")
        if item in result:
            raise ContinuityEvaluationError(code, f"duplicate ID: {item}", path=f"{path}[{index}]")
        result.append(item)
    return result


def _soft_string_sequence(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _non_empty_string(value: Any, path: str, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContinuityEvaluationError(code, f"{path} must be a non-empty string", path=path)
    return value


def _fraction(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ContinuityEvaluationError(
            "EVALUATION_NUMBER_INVALID", f"{path} must be between 0 and 1", path=path
        )
    return float(value)


def _non_negative_number(value: Any, path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ContinuityEvaluationError(
            "EVALUATION_NUMBER_INVALID", f"{path} must be non-negative", path=path
        )
    return value


def _non_negative_integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContinuityEvaluationError(
            "EVALUATION_NUMBER_INVALID", f"{path} must be a non-negative integer", path=path
        )
    return value


def _optional_non_negative_integer(value: Any, path: str) -> int | None:
    return None if value is None else _non_negative_integer(value, path)


def _recall(actual: set[str], expected: set[str]) -> float:
    return len(actual & expected) / len(expected) if expected else 1.0


def _object_id(record: Mapping[str, Any]) -> str:
    for field in (
        "object_id",
        "claim_id",
        "decision_id",
        "question_id",
        "work_item_id",
        "conflict_id",
        "artifact_id",
        "revision_id",
        "skill_id",
    ):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    raise ContinuityEvaluationError(
        "LIFECYCLE_OBJECT_ID_MISSING", "record has no stable object identifier"
    )
