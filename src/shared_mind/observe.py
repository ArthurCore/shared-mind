"""Deterministic pending observation buffers for DEV-102 hook capture."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_bytes
from .product import ProductError, ProductService
from .product_contract import validate_product_object
from .task_trace import TASK_TRACE_VERSION, TaskTraceError, parse_task_trace
from .workspace import MAX_JSON_BYTES, Workspace


OBSERVATION_VERSION = "observation-session@1"
_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}:[a-z0-9][a-z0-9._-]{0,127}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ObservationCapture:
    """Manage one append-only JSONL buffer per opaque session identity."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.root = workspace.root / "observations"

    def buffer_path(self, session_id: str, *, state: str = "pending") -> Path:
        if state not in {"pending", "captured"}:
            raise ProductError("OBSERVATION_STATE_INVALID", f"Unknown observation state: {state}")
        return self.root / state / f"{_session_digest(session_id)}.jsonl"

    def start(
        self,
        session_id: str,
        task_id: str,
        *,
        related_object_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        source_session_id = _require_session(session_id)
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise ProductError("OBSERVATION_TASK_INVALID", "task must be a valid task identifier")
        if isinstance(related_object_ids, (str, bytes)) or any(
            not isinstance(value, str) or not value for value in related_object_ids
        ):
            raise ProductError(
                "OBSERVATION_RELATED_OBJECT_INVALID",
                "related object IDs must be non-empty strings",
            )
        if len(set(related_object_ids)) != len(related_object_ids):
            raise ProductError(
                "OBSERVATION_RELATED_OBJECT_INVALID",
                "related object IDs must be unique",
            )
        digest = _session_digest(source_session_id)
        normalized_session_id = _normalized_session_id(source_session_id, digest)
        header = {
            "object_type": "OBSERVATION_SESSION",
            "observation_version": OBSERVATION_VERSION,
            "source_session_id": source_session_id,
            "session_id": normalized_session_id,
            "trace_id": _trace_id(normalized_session_id, digest),
            "task_id": task_id,
            "related_object_ids": list(related_object_ids),
        }
        content = (canonical_json(header) + "\n").encode("utf-8")
        captured = self.buffer_path(source_session_id, state="captured")
        pending = self.buffer_path(source_session_id, state="pending")
        for path in (captured, pending):
            if path.exists():
                existing_header, _ = self._read_buffer(path)
                if existing_header != header:
                    raise ProductError(
                        "OBSERVATION_SESSION_CONFLICT",
                        f"Session {source_session_id!r} is already bound to different metadata.",
                    )
                return {"status": "UNCHANGED", "path": path.as_posix(), **header}
        _write_new_atomic(pending, content)
        return {"status": "STARTED", "path": pending.as_posix(), **header}

    def append(self, session_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        source_session_id = _require_session(session_id)
        normalized = dict(event) if isinstance(event, Mapping) else event
        issues = validate_product_object(normalized, "TaskTraceEvent")
        if issues:
            raise ProductError(
                "OBSERVATION_EVENT_INVALID",
                "Observation event failed task-trace-event@1 validation.",
                data=issues,
            )
        pending = self.buffer_path(source_session_id, state="pending")
        if not pending.is_file():
            if self.buffer_path(source_session_id, state="captured").is_file():
                raise ProductError(
                    "OBSERVATION_ALREADY_FINALIZED",
                    f"Session {source_session_id!r} has already been finalized.",
                )
            raise ProductError(
                "OBSERVATION_NOT_STARTED",
                f"Session {source_session_id!r} has no pending observation buffer.",
            )
        _, events = self._read_buffer(pending)
        expected_sequence = len(events) + 1
        if normalized["sequence"] != expected_sequence:
            raise ProductError(
                "OBSERVATION_EVENT_SEQUENCE_INVALID",
                f"Expected event sequence {expected_sequence}, got {normalized['sequence']}.",
            )
        line = (canonical_json(normalized) + "\n").encode("utf-8")
        try:
            current_size = pending.stat().st_size
        except OSError as exc:
            raise ProductError("OBSERVATION_BUFFER_READ_FAILED", str(exc)) from exc
        if current_size + len(line) > MAX_JSON_BYTES:
            raise ProductError(
                "OBSERVATION_BUFFER_TOO_LARGE",
                f"Observation buffer exceeds the {MAX_JSON_BYTES}-byte limit.",
            )
        flags = os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(pending, flags)
            try:
                written = os.write(descriptor, line)
                if written != len(line):
                    raise OSError("short append")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ProductError("OBSERVATION_APPEND_FAILED", str(exc)) from exc
        return {
            "status": "APPENDED",
            "path": pending.as_posix(),
            "event_id": normalized["event_id"],
            "sequence": normalized["sequence"],
        }

    def finalize(
        self, session_id: str, service: ProductService | None = None
    ) -> dict[str, Any]:
        source_session_id = _require_session(session_id)
        pending = self.buffer_path(source_session_id, state="pending")
        captured = self.buffer_path(source_session_id, state="captured")
        source = pending if pending.is_file() else captured
        if not source.is_file():
            raise ProductError(
                "OBSERVATION_NOT_STARTED",
                f"Session {source_session_id!r} has no observation buffer.",
            )
        header, events = self._read_buffer(source)
        if not events:
            raise ProductError(
                "OBSERVATION_EMPTY",
                f"Session {source_session_id!r} has no events to finalize.",
            )
        trace = {
            "object_type": "TASK_TRACE",
            "trace_version": TASK_TRACE_VERSION,
            "trace_id": header["trace_id"],
            "task_id": header["task_id"],
            "session_id": header["session_id"],
            "started_at": events[0]["occurred_at"],
            "ended_at": events[-1]["occurred_at"],
            "related_object_ids": header["related_object_ids"],
            "events": events,
        }
        try:
            parsed = parse_task_trace(str(header["task_id"]), trace)
        except TaskTraceError as exc:
            raise ProductError(exc.code, exc.message, data=exc.data) from exc
        if parsed is None:  # pragma: no cover - the constructed trace is always structured
            raise ProductError("OBSERVATION_TRACE_INVALID", "Observation trace was not structured.")
        owns_service = service is None
        product = service if service is not None else ProductService(self.workspace)
        try:
            result = product.post_task_capture(str(header["task_id"]), parsed)
        finally:
            if owns_service:
                product.close()
        if source == pending:
            _archive_no_clobber(pending, captured)
        return result

    @staticmethod
    def _read_buffer(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ProductError("OBSERVATION_BUFFER_READ_FAILED", str(exc)) from exc
        if len(content) > MAX_JSON_BYTES:
            raise ProductError(
                "OBSERVATION_BUFFER_TOO_LARGE",
                f"Observation buffer exceeds the {MAX_JSON_BYTES}-byte limit.",
            )
        try:
            documents = [json.loads(line) for line in content.splitlines()]
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ProductError(
                "OBSERVATION_BUFFER_INVALID", "Observation buffer is not valid JSONL."
            ) from exc
        if not documents or not isinstance(documents[0], dict):
            raise ProductError("OBSERVATION_BUFFER_INVALID", "Observation header is missing.")
        header = documents[0]
        required_header = {
            "object_type",
            "observation_version",
            "source_session_id",
            "session_id",
            "trace_id",
            "task_id",
            "related_object_ids",
        }
        if set(header) != required_header or header.get("object_type") != "OBSERVATION_SESSION":
            raise ProductError("OBSERVATION_BUFFER_INVALID", "Observation header is invalid.")
        if header.get("observation_version") != OBSERVATION_VERSION:
            raise ProductError("OBSERVATION_BUFFER_INVALID", "Observation version is unsupported.")
        events = documents[1:]
        if any(not isinstance(event, dict) for event in events):
            raise ProductError("OBSERVATION_BUFFER_INVALID", "Observation event is invalid.")
        return header, events


def _require_session(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id or len(session_id) > 4096:
        raise ProductError(
            "OBSERVATION_SESSION_INVALID", "session must be a non-empty opaque identifier"
        )
    return session_id


def _session_digest(session_id: str) -> str:
    return sha256_bytes(session_id.encode("utf-8")).split(":", 1)[1][:32]


def _normalized_session_id(session_id: str, digest: str) -> str:
    return session_id if _SEMANTIC_ID.fullmatch(session_id) is not None else f"session:{digest}"


def _trace_id(session_id: str, digest: str) -> str:
    if session_id.startswith("session:"):
        candidate = f"trace:{session_id.split(':', 1)[1]}"
        if _SEMANTIC_ID.fullmatch(candidate) is not None:
            return candidate
    return f"trace:{digest}"


def _write_new_atomic(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".observation-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            if destination.read_bytes() != content:
                raise ProductError(
                    "OBSERVATION_SESSION_CONFLICT",
                    f"Observation destination already contains different bytes: {destination}",
                )
    except ProductError:
        raise
    except OSError as exc:
        raise ProductError("OBSERVATION_START_FAILED", str(exc)) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _archive_no_clobber(pending: Path, captured: Path) -> None:
    captured.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(pending, captured)
    except FileExistsError:
        try:
            if pending.read_bytes() != captured.read_bytes():
                raise ProductError(
                    "OBSERVATION_ARCHIVE_CONFLICT",
                    f"Captured buffer already contains different bytes: {captured}",
                )
        except OSError as exc:
            raise ProductError("OBSERVATION_ARCHIVE_FAILED", str(exc)) from exc
    except OSError as exc:
        raise ProductError("OBSERVATION_ARCHIVE_FAILED", str(exc)) from exc
    try:
        pending.unlink()
    except OSError as exc:
        raise ProductError("OBSERVATION_ARCHIVE_FAILED", str(exc)) from exc


__all__ = ["OBSERVATION_VERSION", "ObservationCapture"]
