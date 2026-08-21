from __future__ import annotations

import io
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from shared_mind.product import ProductService
from shared_mind.workspace import Workspace

from tests.test_observe import SESSION_ID, TASK_ID, task_trace_event


def run_hook(arguments: list[str], payload: dict) -> tuple[int, str]:
    from shared_mind.adapters.claude_code_hooks import main

    errors = io.StringIO()
    exit_code = main(arguments, stdin=io.StringIO(json.dumps(payload)), stderr=errors)
    return exit_code, errors.getvalue()


class ClaudeCodeHooksTest(unittest.TestCase):
    def test_post_tool_use_lazily_starts_with_payload_or_session_task(self) -> None:
        cases = (
            ("session:dev-102-lazy-explicit", "DEV-102", "DEV-102"),
            ("session:dev-102-lazy-fallback", None, None),
        )
        for session_id, payload_task_id, expected_task_id in cases:
            with self.subTest(session_id=session_id):
                with tempfile.TemporaryDirectory() as temporary:
                    workspace_root = Path(temporary) / "workspace"
                    Workspace.initialize(workspace_root, purpose="DEV-102 lazy hook test")
                    payload = {
                        "session_id": session_id,
                        "cwd": str(workspace_root),
                        "hook_event_name": "PostToolUse",
                        "tool_name": "Read",
                        "tool_input": {"file_path": "README.md"},
                        "tool_response": {"ok": True},
                        "tool_use_id": "tool_use_dev102_lazy_001",
                        "timestamp": "2026-08-21T03:04:05Z",
                    }
                    if payload_task_id is not None:
                        payload["task_id"] = payload_task_id

                    append_exit, append_errors = run_hook(
                        ["append", "--workspace", str(workspace_root)], payload
                    )
                    finalize_exit, finalize_errors = run_hook(
                        ["finalize", "--workspace", str(workspace_root)],
                        {"session_id": session_id},
                    )

                    self.assertEqual(0, append_exit)
                    self.assertEqual("", append_errors)
                    self.assertEqual(0, finalize_exit)
                    self.assertEqual("", finalize_errors)
                    service = ProductService.open(workspace_root)
                    try:
                        batches = service.store.list_batches()
                        self.assertEqual(1, len(batches))
                        revision_id = service.store.list_ingest_items(
                            batches[0]["batch_id"]
                        )[0]["revision_id"]
                        restored = json.loads(
                            service.tool_call(
                                "read_source_span", {"revision_id": revision_id}
                            )["excerpt"]
                        )
                    finally:
                        service.close()
                    if expected_task_id is None:
                        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
                        expected_task_id = f"observation-{digest}"
                    self.assertEqual(expected_task_id, restored["task_id"])
                    self.assertEqual("2026-08-21T03:04:05Z", restored["started_at"])
                    self.assertEqual("2026-08-21T03:04:05Z", restored["ended_at"])

    def test_missing_workspace_is_fail_open_and_records_failure_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            payload = {
                "cwd": str(project),
                "session_id": SESSION_ID,
                "event": task_trace_event(1, "TOOL"),
            }
            previous = Path.cwd()
            os.chdir(project)
            try:
                exit_code, errors = run_hook(["append"], payload)
            finally:
                os.chdir(previous)

            failed = list((project / "observations" / "failed").glob("*.json"))
            self.assertEqual(0, exit_code)
            self.assertEqual(1, len(errors.strip().splitlines()))
            self.assertEqual(1, len(failed))
            self.assertEqual("WORKSPACE_NOT_FOUND", json.loads(failed[0].read_text())["code"])
            self.assertFalse((project / ".shared-mind").exists())
            self.assertFalse(list(project.rglob("*.sqlite3")))

    def test_adapter_preserves_input_event_order_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary) / "workspace"
            workspace = Workspace.initialize(workspace_root, purpose="DEV-102 hook test")
            from shared_mind.observe import ObservationCapture

            ObservationCapture(workspace).start(SESSION_ID, TASK_ID)
            events = [
                task_trace_event(1, "TOOL"),
                task_trace_event(2, "RESULT"),
            ]
            events[0]["occurred_at"] = "2026-08-20T23:59:59Z"
            events[1]["occurred_at"] = "2026-08-21T01:02:03Z"

            for event in events:
                exit_code, errors = run_hook(
                    ["append", "--workspace", str(workspace_root)],
                    {"session_id": SESSION_ID, "event": event},
                )
                self.assertEqual(0, exit_code)
                self.assertEqual("", errors)
            exit_code, errors = run_hook(
                ["finalize", "--workspace", str(workspace_root)],
                {"session_id": SESSION_ID},
            )
            self.assertEqual(0, exit_code)
            self.assertEqual("", errors)

            service = ProductService.open(workspace_root)
            try:
                batches = service.store.list_batches()
                self.assertEqual(1, len(batches))
                revision_id = service.store.list_ingest_items(batches[0]["batch_id"])[0][
                    "revision_id"
                ]
                restored = json.loads(
                    service.tool_call("read_source_span", {"revision_id": revision_id})[
                        "excerpt"
                    ]
                )
            finally:
                service.close()
            self.assertEqual(
                [event["event_id"] for event in events],
                [event["event_id"] for event in restored["events"]],
            )
            self.assertEqual(
                [event["occurred_at"] for event in events],
                [event["occurred_at"] for event in restored["events"]],
            )
            self.assertEqual(events[0]["occurred_at"], restored["started_at"])
            self.assertEqual(events[-1]["occurred_at"], restored["ended_at"])


if __name__ == "__main__":
    unittest.main()
