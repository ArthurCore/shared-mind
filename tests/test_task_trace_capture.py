from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_mind.product import ProductError, ProductService

from tests.product_support import ProductTestCase


TRACE_ID = "trace:dev-081-session-001"
TASK_ID = "DEV-081"
SESSION_ID = "session:dev-081-cold-start-001"


def task_trace() -> dict:
    event_types = ("TASK", "TOOL", "RESULT", "DECISION", "FAILURE", "TEST")
    events = []
    for sequence, event_type in enumerate(event_types, start=1):
        events.append(
            {
                "object_type": "TASK_TRACE_EVENT",
                "event_version": "task-trace-event@1",
                "event_id": f"trace_event_{sequence:024d}",
                "sequence": sequence,
                "event_type": event_type,
                "occurred_at": f"2026-08-15T00:00:{sequence:02d}Z",
                "summary": f"DEV-081 {event_type.lower()} evidence",
                "details": {"exit_code": 1 if event_type == "FAILURE" else 0},
            }
        )
    return {
        "object_type": "TASK_TRACE",
        "trace_version": "task-trace@1",
        "trace_id": TRACE_ID,
        "task_id": TASK_ID,
        "session_id": SESSION_ID,
        "started_at": "2026-08-15T00:00:01Z",
        "ended_at": "2026-08-15T00:00:06Z",
        "related_object_ids": ["workitem_extract_aa0a56c4dbee6f1a7734bd3c"],
        "events": events,
    }


class TaskTraceCaptureTest(ProductTestCase):
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

    def _trace_files(self) -> list[Path]:
        root = self.workspace.source_root / "task-traces"
        return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []

    def test_capture_is_versioned_immutable_and_restorable_in_a_fresh_session(self) -> None:
        result = self.service.post_task_capture(TASK_ID, task_trace())
        receipt = result["capture_receipt"]

        self.assertEqual("TASK_TRACE_CAPTURE_RECEIPT", receipt["object_type"])
        self.assertEqual("task-trace-capture@1", receipt["capture_version"])
        self.assertEqual("CAPTURED", receipt["status"])
        self.assertEqual(TRACE_ID, receipt["trace_id"])
        self.assertEqual(6, receipt["event_count"])
        self.assertEqual(
            ["DECISION", "FAILURE", "RESULT", "TASK", "TEST", "TOOL"],
            receipt["event_types"],
        )
        self.assertEqual("2026-08-15T00:00:01Z", receipt["started_at"])
        self.assertEqual("2026-08-15T00:00:06Z", receipt["ended_at"])

        fresh = ProductService.open(self.workspace_root)
        try:
            span = fresh.tool_call(
                "read_source_span", {"revision_id": receipt["source_revision_id"]}
            )
            restored = json.loads(span["excerpt"])
            self.assertEqual(task_trace(), restored)
            self.assertEqual(
                "2026-08-15T00:00:01Z",
                span["source_revision"]["captured_at"],
            )
            search = fresh.search("failure evidence", kinds=["SOURCE_TEXT"])
            self.assertIn(
                f"source-text:{receipt['source_revision_id']}",
                [item["document_id"] for item in search["results"]],
            )
        finally:
            fresh.close()

    def test_identical_capture_is_idempotent_without_new_ledger_or_audit_rows(self) -> None:
        first = self.service.post_task_capture(TASK_ID, task_trace())
        before = self._kernel_snapshot()
        audit_before = self.service.store.verify_audit()["count"]

        second = self.service.post_task_capture(TASK_ID, task_trace())

        self.assertEqual("UNCHANGED", second["capture_receipt"]["status"])
        self.assertEqual(
            first["capture_receipt"]["source_revision_id"],
            second["capture_receipt"]["source_revision_id"],
        )
        self.assertEqual(before, self._kernel_snapshot())
        self.assertEqual(audit_before, self.service.store.verify_audit()["count"])

    def test_same_trace_identity_with_changed_content_is_rejected_without_mutation(self) -> None:
        self.service.post_task_capture(TASK_ID, task_trace())
        before = self._kernel_snapshot()
        audit_before = self.service.store.verify_audit()["count"]
        changed = task_trace()
        changed["events"][2]["summary"] = "forged replacement result"

        with self.assertRaises(ProductError) as caught:
            self.service.post_task_capture(TASK_ID, changed)

        self.assertEqual("TASK_TRACE_IMMUTABLE_CONFLICT", caught.exception.code)
        self.assertEqual(before, self._kernel_snapshot())
        self.assertEqual(audit_before, self.service.store.verify_audit()["count"])

    def test_malformed_trace_is_rejected_before_file_or_canonical_mutation(self) -> None:
        malformed_cases = (
            "{not-json",
            {**task_trace(), "task_id": "DEV-999"},
            {**task_trace(), "unknown": True},
            {**task_trace(), "events": []},
        )
        duplicate_event = task_trace()
        duplicate_event["events"][1]["event_id"] = duplicate_event["events"][0]["event_id"]
        malformed_cases += (duplicate_event,)
        wrong_sequence = task_trace()
        wrong_sequence["events"][2]["sequence"] = 99
        malformed_cases += (wrong_sequence,)
        before = self._kernel_snapshot()

        for candidate in malformed_cases:
            with self.subTest(candidate_type=type(candidate).__name__):
                with self.assertRaises(ProductError) as caught:
                    self.service.post_task_capture(TASK_ID, candidate)
                self.assertIn(
                    caught.exception.code,
                    {"TASK_TRACE_MALFORMED", "TASK_TRACE_INVALID", "TASK_ID_MISMATCH"},
                )

        self.assertEqual(before, self._kernel_snapshot())
        self.assertEqual([], self._trace_files())

    def test_partial_failure_retries_same_immutable_source_without_duplicate_commit(self) -> None:
        original_extract = self.service.extract
        with patch.object(
            self.service,
            "extract",
            side_effect=ProductError("INJECTED_CAPTURE_FAILURE", "after source registration"),
        ):
            with self.assertRaises(ProductError) as caught:
                self.service.post_task_capture(TASK_ID, task_trace())
        self.assertEqual("INJECTED_CAPTURE_FAILURE", caught.exception.code)
        after_failure = self._kernel_snapshot()
        self.assertEqual(1, after_failure["ledger"])
        self.assertEqual(1, after_failure["sources"])

        with patch.object(self.service, "extract", wraps=original_extract):
            retried = self.service.post_task_capture(TASK_ID, task_trace())

        self.assertEqual("UNCHANGED", retried["capture_receipt"]["status"])
        self.assertEqual(after_failure, self._kernel_snapshot())
        self.assertEqual(TRACE_ID, retried["capture_receipt"]["trace_id"])

    def test_atomic_write_failure_leaves_no_partial_trace_and_retry_succeeds(self) -> None:
        before = self._kernel_snapshot()
        with patch("shared_mind.product.os.link", side_effect=OSError("injected link failure")):
            with self.assertRaises(ProductError) as caught:
                self.service.post_task_capture(TASK_ID, task_trace())
        self.assertEqual("TASK_TRACE_WRITE_FAILED", caught.exception.code)
        self.assertEqual(before, self._kernel_snapshot())
        self.assertEqual([], self._trace_files())

        retried = self.service.post_task_capture(TASK_ID, task_trace())
        self.assertEqual("CAPTURED", retried["capture_receipt"]["status"])

    def test_timestamp_and_event_order_are_preserved_exactly(self) -> None:
        trace = task_trace()
        trace["events"][3]["occurred_at"] = "2026-08-14T23:59:59Z"
        result = self.service.post_task_capture(TASK_ID, trace)
        span = self.service.tool_call(
            "read_source_span",
            {"revision_id": result["capture_receipt"]["source_revision_id"]},
        )
        restored = json.loads(span["excerpt"])

        self.assertEqual(
            [event["occurred_at"] for event in trace["events"]],
            [event["occurred_at"] for event in restored["events"]],
        )
        self.assertEqual(
            list(range(1, 7)),
            [event["sequence"] for event in restored["events"]],
        )
        self.assertEqual(trace["started_at"], span["source_revision"]["captured_at"])

    def test_roadmap_marks_dev_080_and_081_done_and_later_work_todo(self) -> None:
        roadmap = (Path(__file__).resolve().parents[1] / "ROADMAP.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("DEV-080 — Shared Mind Self-Dogfooding", roadmap)
        self.assertIn("**상태: DONE**", roadmap)
        self.assertIn("DEV-081 — Real Session Capture", roadmap)
        dev_081 = roadmap.split("### DEV-081 — Real Session Capture", 1)[1].split(
            "### DEV-082~086", 1
        )[0]
        self.assertIn("**상태: DONE**", dev_081)
        for dev in range(82, 87):
            self.assertRegex(roadmap, rf"DEV-{dev:03d}[^\n]*TODO")


if __name__ == "__main__":
    unittest.main()
