from __future__ import annotations

import json
import io
from pathlib import Path
from unittest.mock import patch

from shared_mind.product import ProductError, ProductService
from shared_mind.product_cli import EXIT_OK, main as product_main
from shared_mind.workspace import Workspace

from tests.product_support import ProductTestCase


TASK_ID = "DEV-102"
SESSION_ID = "session:dev-102-observation-001"
EVENT_TYPES = ("TASK", "TOOL", "RESULT", "DECISION", "FAILURE", "TEST")


def task_trace_event(sequence: int, event_type: str) -> dict:
    return {
        "object_type": "TASK_TRACE_EVENT",
        "event_version": "task-trace-event@1",
        "event_id": f"trace_event_dev102_{sequence:08d}",
        "sequence": sequence,
        "event_type": event_type,
        "occurred_at": f"2026-08-21T00:00:{sequence:02d}Z",
        "summary": f"DEV-102 {event_type.lower()} observation",
        "details": {"sequence": sequence},
    }


def observation_capture(workspace):
    from shared_mind.observe import ObservationCapture

    return ObservationCapture(workspace)


class ObservationCaptureTest(ProductTestCase):
    def _product_cli(self, *arguments: str) -> tuple[int, dict]:
        output = io.StringIO()
        exit_code = product_main(
            ["--workspace", str(self.workspace_root), *arguments], stdout=output
        )
        return exit_code, json.loads(output.getvalue())

    def _kernel_snapshot(self) -> dict[str, int | str]:
        kernel = self.workspace.open_kernel()
        try:
            return {
                "ledger": kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
                "receipts": kernel.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
                "sources": kernel.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "state_root": kernel.state_root(),
            }
        finally:
            kernel.close()

    def _start_complete_buffer(self):
        capture = observation_capture(self.workspace)
        capture.start(SESSION_ID, TASK_ID)
        for sequence, event_type in enumerate(EVENT_TYPES, start=1):
            capture.append(SESSION_ID, task_trace_event(sequence, event_type))
        return capture

    def _capture_at(self, session_id: str, occurred_at: str) -> tuple[object, Path]:
        capture = observation_capture(self.workspace)
        capture.start(session_id, TASK_ID)
        event = task_trace_event(1, "TOOL")
        event["event_id"] = f"trace_event_{session_id.split(':', 1)[1]}"
        event["occurred_at"] = occurred_at
        capture.append(session_id, event)
        result = capture.finalize(session_id, self.service)
        return result, capture.buffer_path(session_id, state="captured")

    def test_start_is_idempotent_and_creates_exactly_one_pending_buffer(self) -> None:
        first_exit, first_response = self._product_cli(
            "observe", "start", "--session", SESSION_ID, "--task", TASK_ID
        )
        self.assertEqual(EXIT_OK, first_exit, first_response)
        self.assertEqual("OBSERVATION_STARTED", first_response["code"])
        first = first_response["data"]
        first_bytes = Path(first["path"]).read_bytes()
        second_exit, second_response = self._product_cli(
            "observe", "start", "--session", SESSION_ID, "--task", TASK_ID
        )
        self.assertEqual(EXIT_OK, second_exit, second_response)
        self.assertEqual("OBSERVATION_STARTED", second_response["code"])
        second = second_response["data"]

        pending = list((self.workspace_root / "observations" / "pending").glob("*.jsonl"))
        self.assertEqual("STARTED", first["status"])
        self.assertEqual("UNCHANGED", second["status"])
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(first_bytes, Path(second["path"]).read_bytes())
        self.assertEqual(1, len(pending))

    def test_six_event_types_finalize_through_dev_081_and_restore_fresh(self) -> None:
        capture = self._start_complete_buffer()

        result = capture.finalize(SESSION_ID, self.service)
        receipt = result["capture_receipt"]

        self.assertEqual("CAPTURED", receipt["status"])
        self.assertEqual(6, receipt["event_count"])
        self.assertFalse(list((self.workspace_root / "observations" / "pending").glob("*")))
        self.assertEqual(
            1,
            len(list((self.workspace_root / "observations" / "captured").glob("*.jsonl"))),
        )
        fresh = ProductService.open(self.workspace_root)
        try:
            span = fresh.tool_call(
                "read_source_span", {"revision_id": receipt["source_revision_id"]}
            )
            restored = json.loads(span["excerpt"])
            self.assertEqual(TASK_ID, restored["task_id"])
            self.assertEqual(SESSION_ID, restored["session_id"])
            self.assertEqual(list(EVENT_TYPES), [item["event_type"] for item in restored["events"]])
        finally:
            fresh.close()

    def test_repeated_finalize_is_unchanged_without_ledger_or_audit_growth(self) -> None:
        capture = self._start_complete_buffer()
        first = capture.finalize(SESSION_ID, self.service)
        kernel_before = self._kernel_snapshot()
        audit_before = self.service.store.verify_audit()["count"]

        second = capture.finalize(SESSION_ID, self.service)

        self.assertEqual("UNCHANGED", second["capture_receipt"]["status"])
        self.assertEqual(
            first["capture_receipt"]["source_revision_id"],
            second["capture_receipt"]["source_revision_id"],
        )
        self.assertEqual(kernel_before, self._kernel_snapshot())
        self.assertEqual(audit_before, self.service.store.verify_audit()["count"])

    def test_invalid_append_does_not_change_pending_buffer_bytes(self) -> None:
        capture = observation_capture(self.workspace)
        started = capture.start(SESSION_ID, TASK_ID)
        path = Path(started["path"])
        before = path.read_bytes()
        invalid = task_trace_event(1, "TOOL")
        invalid["summary"] = ""

        with self.assertRaises(ProductError) as caught:
            capture.append(SESSION_ID, invalid)

        self.assertEqual("OBSERVATION_EVENT_INVALID", caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_registration_failure_preserves_buffer_and_retry_reuses_source(self) -> None:
        capture = self._start_complete_buffer()
        pending = Path(capture.buffer_path(SESSION_ID, state="pending"))
        original_extract = self.service.extract

        with patch.object(
            self.service,
            "extract",
            side_effect=ProductError("INJECTED_CAPTURE_FAILURE", "after registration"),
        ):
            with self.assertRaises(ProductError) as caught:
                capture.finalize(SESSION_ID, self.service)

        self.assertEqual("INJECTED_CAPTURE_FAILURE", caught.exception.code)
        self.assertTrue(pending.is_file())
        failed_batches = self.service.store.list_batches()
        self.assertEqual(1, len(failed_batches))
        failed_items = self.service.store.list_ingest_items(failed_batches[0]["batch_id"])
        self.assertEqual(1, len(failed_items))

        with patch.object(self.service, "extract", wraps=original_extract):
            retried = capture.finalize(SESSION_ID, self.service)

        self.assertEqual("UNCHANGED", retried["capture_receipt"]["status"])
        self.assertEqual(failed_batches[0]["batch_id"], retried["batch"]["batch_id"])
        self.assertEqual(
            failed_items[0]["revision_id"], retried["capture_receipt"]["source_revision_id"]
        )
        self.assertFalse(pending.exists())

    def test_prune_removes_only_captured_buffers_strictly_before_cutoff(self) -> None:
        capture = observation_capture(self.workspace)
        _, old = self._capture_at(
            "session:dev-102-prune-old", "2026-08-21T00:59:59Z"
        )
        _, exact = self._capture_at(
            "session:dev-102-prune-exact", "2026-08-21T01:00:00Z"
        )
        _, newer = self._capture_at(
            "session:dev-102-prune-newer", "2026-08-21T01:00:01Z"
        )
        pending_session = "session:dev-102-prune-pending"
        capture.start(pending_session, TASK_ID)
        pending_event = task_trace_event(1, "TOOL")
        pending_event["event_id"] = "trace_event_dev102_prune_pending"
        pending_event["occurred_at"] = "2026-08-20T00:00:00Z"
        capture.append(pending_session, pending_event)
        pending = capture.buffer_path(pending_session, state="pending")
        canonical_before = {
            "kernel": self._kernel_snapshot(),
            "audit": self.service.store.verify_audit(),
            "batches": self.service.store.list_batches(),
        }

        exit_code, response = self._product_cli(
            "observe", "prune", "--before", "2026-08-21T01:00:00Z"
        )

        self.assertEqual(EXIT_OK, exit_code, response)
        self.assertEqual("OBSERVATIONS_PRUNED", response["code"])
        self.assertEqual(
            {
                "before": "2026-08-21T01:00:00Z",
                "removed": 1,
                "retained": 2,
                "scanned": 3,
                "status": "PRUNED",
            },
            response["data"],
        )
        self.assertFalse(old.exists())
        self.assertTrue(exact.is_file())
        self.assertTrue(newer.is_file())
        self.assertTrue(pending.is_file())
        self.assertEqual(
            canonical_before,
            {
                "kernel": self._kernel_snapshot(),
                "audit": self.service.store.verify_audit(),
                "batches": self.service.store.list_batches(),
            },
        )

    def test_prune_rejects_non_rfc3339_cutoff_without_file_changes(self) -> None:
        _, captured = self._capture_at(
            "session:dev-102-prune-invalid", "2026-08-20T00:00:00Z"
        )
        before = captured.read_bytes()

        exit_code, response = self._product_cli(
            "observe", "prune", "--before", "2026-08-21"
        )

        self.assertNotEqual(EXIT_OK, exit_code)
        self.assertEqual("OBSERVATION_CUTOFF_INVALID", response["code"])
        self.assertEqual(before, captured.read_bytes())

    def test_identical_observation_finalization_is_cross_workspace_deterministic(
        self,
    ) -> None:
        results = []
        for index in (1, 2):
            workspace = Workspace.initialize(
                self.base / f"determinism-{index}", purpose="DEV-102 determinism"
            )
            service = ProductService(workspace)
            try:
                capture = observation_capture(workspace)
                capture.start(SESSION_ID, TASK_ID)
                for sequence, event_type in enumerate(EVENT_TYPES, start=1):
                    capture.append(
                        SESSION_ID, task_trace_event(sequence, event_type)
                    )
                result = capture.finalize(SESSION_ID, service)
                receipt = result["capture_receipt"]
                source_bytes = (
                    workspace.root / receipt["destination"]
                ).read_bytes()
                results.append(
                    (
                        source_bytes,
                        receipt["content_hash"],
                        receipt["trace_id"],
                    )
                )
            finally:
                service.close()

        self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    import unittest

    unittest.main()
