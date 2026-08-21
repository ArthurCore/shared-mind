from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from shared_mind.product import ProductError, ProductService

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

    def test_start_is_idempotent_and_creates_exactly_one_pending_buffer(self) -> None:
        capture = observation_capture(self.workspace)

        first = capture.start(SESSION_ID, TASK_ID)
        first_bytes = Path(first["path"]).read_bytes()
        second = capture.start(SESSION_ID, TASK_ID)

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


if __name__ == "__main__":
    import unittest

    unittest.main()
