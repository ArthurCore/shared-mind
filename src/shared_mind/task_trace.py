"""Strict, agent-neutral task trace contracts for real session capture."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import canonical_json
from .product_contract import validate_product_object
from .workspace import MAX_JSON_BYTES, MAX_JSON_DEPTH


TASK_TRACE_VERSION = "task-trace@1"
TASK_TRACE_EVENT_VERSION = "task-trace-event@1"
TASK_TRACE_CAPTURE_VERSION = "task-trace-capture@1"


class TaskTraceError(Exception):
    """Stable task-trace validation failure."""

    def __init__(self, code: str, message: str, *, data: Any | None = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


def parse_task_trace(
    task_id: str, trace: str | Mapping[str, Any] | Sequence[Any]
) -> dict[str, Any] | None:
    """Return a strict task trace, or ``None`` for the legacy text surface.

    Plain directive text and legacy event sequences retain the DEV-073 path.
    JSON-looking strings and mappings are always treated as the strict DEV-081
    contract, so malformed structured input cannot silently become a source.
    """

    candidate: Any
    if isinstance(trace, str):
        stripped = trace.strip()
        if not stripped.startswith(("{", "[")):
            return None
        try:
            candidate = json.loads(stripped)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise TaskTraceError("TASK_TRACE_MALFORMED", "Task trace is not valid JSON.") from exc
    elif isinstance(trace, Mapping):
        candidate = dict(trace)
    else:
        return None

    if not isinstance(candidate, Mapping):
        raise TaskTraceError("TASK_TRACE_INVALID", "Task trace must be a JSON object.")
    normalized = dict(candidate)
    if _json_depth_exceeds(normalized, MAX_JSON_DEPTH):
        raise TaskTraceError(
            "TASK_TRACE_INVALID",
            f"Task trace exceeds the maximum JSON depth of {MAX_JSON_DEPTH}.",
        )
    try:
        encoded = (canonical_json(normalized) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise TaskTraceError("TASK_TRACE_INVALID", "Task trace is not canonical JSON.") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise TaskTraceError(
            "TASK_TRACE_INVALID",
            f"Task trace exceeds the {MAX_JSON_BYTES}-byte limit.",
        )
    issues = validate_product_object(normalized, "TaskTrace")
    if issues:
        raise TaskTraceError(
            "TASK_TRACE_INVALID", "Task trace failed contract validation.", data=issues
        )
    if normalized["task_id"] != task_id:
        raise TaskTraceError(
            "TASK_ID_MISMATCH",
            f"Trace task_id {normalized['task_id']!r} does not match {task_id!r}.",
        )
    events = normalized["events"]
    expected_sequences = list(range(1, len(events) + 1))
    actual_sequences = [int(event["sequence"]) for event in events]
    if actual_sequences != expected_sequences:
        raise TaskTraceError(
            "TASK_TRACE_INVALID", "Task trace event sequences must be contiguous from 1."
        )
    event_ids = [str(event["event_id"]) for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise TaskTraceError("TASK_TRACE_INVALID", "Task trace event IDs must be unique.")
    if normalized["started_at"] > normalized["ended_at"]:
        raise TaskTraceError(
            "TASK_TRACE_INVALID", "Task trace started_at must not follow ended_at."
        )
    return normalized


def canonical_task_trace_bytes(trace: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(trace)) + "\n").encode("utf-8")


def _json_depth_exceeds(value: Any, limit: int) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    seen: set[int] = set()
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, (Mapping, list, tuple)):
            continue
        identity = id(current)
        if identity in seen:
            raise TaskTraceError("TASK_TRACE_INVALID", "Task trace contains a cycle.")
        seen.add(identity)
        if depth > limit:
            return True
        children = current.values() if isinstance(current, Mapping) else current
        stack.extend((child, depth + 1) for child in children)
    return False


__all__ = [
    "TASK_TRACE_CAPTURE_VERSION",
    "TASK_TRACE_EVENT_VERSION",
    "TASK_TRACE_VERSION",
    "TaskTraceError",
    "canonical_task_trace_bytes",
    "parse_task_trace",
]
