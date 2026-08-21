"""Fail-open Claude Code hook entrypoint for DEV-102 observation capture."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from ..canonical import canonical_json, sha256_bytes
from ..observe import ObservationCapture
from ..product import ProductError, ProductService
from ..workspace import MAX_JSON_BYTES, Workspace


_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _HookArgumentError(Exception):
    pass


class _HookParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _HookArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _HookParser(prog="shared-mind-claude-hook", add_help=False)
    parser.add_argument("action", choices=("append", "finalize"))
    parser.add_argument("--workspace")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one hook action and always return zero to the calling Agent."""

    errors = stderr if stderr is not None else sys.stderr
    payload: dict[str, Any] = {}
    action = "unknown"
    failure_root = Path.cwd()
    try:
        arguments = build_parser().parse_args(argv)
        action = arguments.action
        raw = (stdin if stdin is not None else sys.stdin).read(MAX_JSON_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
            raise ProductError("HOOK_PAYLOAD_TOO_LARGE", "Hook payload exceeds the JSON limit.")
        parsed = json.loads(raw)
        if not isinstance(parsed, Mapping):
            raise ProductError("HOOK_PAYLOAD_INVALID", "Hook payload must be a JSON object.")
        payload = dict(parsed)
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ProductError("HOOK_SESSION_MISSING", "Hook payload has no session_id.")
        workspace = Workspace.open(arguments.workspace or Path.cwd())
        failure_root = workspace.root
        capture = ObservationCapture(workspace)
        if action == "append":
            _lazy_start(payload, session_id, capture)
            capture.append(session_id, _event_from_payload(payload, capture))
        else:
            service = ProductService(workspace)
            try:
                capture.finalize(session_id, service)
            finally:
                service.close()
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__.upper())
        message = getattr(exc, "message", str(exc))
        _record_failure(
            failure_root,
            action=action,
            code=str(code),
            message=str(message),
            session_id=payload.get("session_id"),
        )
        summary = " ".join(str(message).splitlines()) or str(code)
        errors.write(f"shared-mind hook {action} skipped: {code}: {summary}\n")
    return 0


def _lazy_start(
    payload: Mapping[str, Any], session_id: str, capture: ObservationCapture
) -> None:
    pending = capture.buffer_path(session_id, state="pending")
    captured = capture.buffer_path(session_id, state="captured")
    if pending.is_file() or captured.is_file():
        return
    candidate = payload.get("task_id")
    if not isinstance(candidate, str) or _TASK_ID.fullmatch(candidate) is None:
        digest = sha256_bytes(session_id.encode("utf-8")).split(":", 1)[1][:32]
        candidate = f"observation-{digest}"
    capture.start(session_id, candidate)


def _event_from_payload(
    payload: Mapping[str, Any], capture: ObservationCapture
) -> Mapping[str, Any]:
    event = payload.get("event")
    if isinstance(event, Mapping):
        return dict(event)
    timestamp = payload.get("occurred_at", payload.get("timestamp"))
    if not isinstance(timestamp, str) or not timestamp:
        raise ProductError(
            "HOOK_TIMESTAMP_MISSING",
            "Hook payload has no original occurred_at or timestamp.",
        )
    session_id = str(payload["session_id"])
    pending = capture.buffer_path(session_id, state="pending")
    _, existing_events = capture._read_buffer(pending)
    sequence = payload.get("sequence", len(existing_events) + 1)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise ProductError("HOOK_SEQUENCE_INVALID", "Hook sequence must be an integer.")
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        digest = sha256_bytes(canonical_json(dict(payload)).encode("utf-8")).split(":", 1)[1]
        event_id = f"trace_event_{digest[:24]}"
    tool_name = payload.get("tool_name")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary:
        summary = f"PostToolUse {tool_name}" if isinstance(tool_name, str) else "PostToolUse"
    return {
        "object_type": "TASK_TRACE_EVENT",
        "event_version": "task-trace-event@1",
        "event_id": event_id,
        "sequence": sequence,
        "event_type": "TOOL",
        "occurred_at": timestamp,
        "summary": summary,
        "details": {
            "hook_event_name": payload.get("hook_event_name", "PostToolUse"),
            "tool_name": tool_name,
            "tool_input": payload.get("tool_input"),
            "tool_response": payload.get("tool_response"),
            "tool_use_id": payload.get("tool_use_id"),
        },
    }


def _record_failure(
    root: Path,
    *,
    action: str,
    code: str,
    message: str,
    session_id: Any,
) -> None:
    record = {
        "object_type": "OBSERVATION_HOOK_FAILURE",
        "failure_version": "observation-hook-failure@1",
        "action": action,
        "code": code,
        "message": message,
        "session_id": session_id if isinstance(session_id, str) else None,
    }
    content = (canonical_json(record) + "\n").encode("utf-8")
    digest = sha256_bytes(content).split(":", 1)[1][:32]
    failed_root = root.resolve() / "observations" / "failed"
    destination = failed_root / f"failure-{digest}.json"
    try:
        failed_root.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        try:
            written = os.write(descriptor, content)
            if written == len(content):
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        pass
    except OSError:
        pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
