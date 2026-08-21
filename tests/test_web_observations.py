from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from shared_mind.web_control import WebControlApplication, create_server

from tests.product_support import ProductTestCase


class WebObservationTest(ProductTestCase):
    def _capture(
        self, index: int, *, occurred_at: str | None = None
    ) -> tuple[dict, dict]:
        timestamp = occurred_at or f"2026-08-21T04:00:{index:02d}Z"
        trace = {
            "object_type": "TASK_TRACE",
            "trace_version": "task-trace@1",
            "trace_id": f"trace:dev-103-observation-{index:03d}",
            "task_id": "DEV-103",
            "session_id": f"session:dev-103-observation-{index:03d}",
            "started_at": timestamp,
            "ended_at": timestamp,
            "related_object_ids": [],
            "events": [
                {
                    "object_type": "TASK_TRACE_EVENT",
                    "event_version": "task-trace-event@1",
                    "event_id": f"trace_event_dev103_{index:04d}_0001",
                    "sequence": 1,
                    "event_type": "TOOL",
                    "occurred_at": timestamp,
                    "summary": f"DEV-103 observation {index}",
                    "details": {"received_index": index},
                }
            ],
        }
        result = self.service.post_task_capture("DEV-103", trace)
        return trace, result["capture_receipt"]

    def _get(self, target: str) -> tuple[int, str, dict | bytes]:
        status, content_type, body = WebControlApplication(self.service).handle(
            "GET", target
        )
        if content_type.startswith("application/json"):
            return status, content_type, json.loads(body)
        return status, content_type, body

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

    def test_list_and_detail_restore_exact_canonical_trace_events(self) -> None:
        trace, receipt = self._capture(1)

        list_status, list_type, listed = self._get("/api/observations?limit=10")
        self.assertEqual(200, list_status)
        self.assertTrue(list_type.startswith("application/json"))
        assert isinstance(listed, dict)
        self.assertEqual("OBSERVATIONS_LISTED", listed["code"])
        self.assertEqual(1, listed["data"]["count"])
        observation = listed["data"]["observations"][0]
        self.assertEqual(receipt, observation["receipt"])
        self.assertIsInstance(observation["cursor"], int)

        detail_status, _, detail = self._get(
            f"/api/observations/{receipt['trace_id']}"
        )
        self.assertEqual(200, detail_status)
        assert isinstance(detail, dict)
        self.assertEqual("OBSERVATION_SHOWN", detail["code"])
        self.assertEqual(receipt, detail["data"]["receipt"])
        self.assertEqual(trace, detail["data"]["trace"])
        self.assertEqual(trace["events"], detail["data"]["events"])
        span = self.service.retrieval.read_source_span(receipt["source_revision_id"])
        self.assertEqual(json.loads(span["excerpt"]), detail["data"]["trace"])

    def test_sse_stream_emits_new_capture_receipt_after_cursor(self) -> None:
        self._capture(1)
        _, _, baseline = self._get("/api/observations?limit=10")
        assert isinstance(baseline, dict)
        after = baseline["data"]["next_cursor"]
        _, receipt = self._capture(2)

        status, content_type, body = self._get(
            f"/api/observations/stream?after={after}"
        )

        self.assertEqual(200, status)
        self.assertEqual("text/event-stream; charset=utf-8", content_type)
        assert isinstance(body, bytes)
        text = body.decode("utf-8")
        data_lines = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
        self.assertEqual(1, len(data_lines))
        event = json.loads(data_lines[0])
        self.assertEqual(receipt, event["receipt"])
        self.assertIn(f"id: {event['cursor']}", text)
        self.assertIn("event: observation", text)

    def test_cursor_pagination_is_received_order_without_duplicate_or_omission(self) -> None:
        expected = []
        for index, second in enumerate((5, 4, 3, 2, 1), start=1):
            _, receipt = self._capture(
                index, occurred_at=f"2026-08-21T04:00:{second:02d}Z"
            )
            expected.append(receipt["trace_id"])

        after = 0
        actual: list[str] = []
        cursors: list[int] = []
        while True:
            status, _, response = self._get(
                f"/api/observations?limit=2&after={after}"
            )
            self.assertEqual(200, status)
            assert isinstance(response, dict)
            page = response["data"]
            actual.extend(item["receipt"]["trace_id"] for item in page["observations"])
            cursors.extend(item["cursor"] for item in page["observations"])
            after = page["next_cursor"]
            if not page["has_more"]:
                break

        self.assertEqual(expected, actual)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(cursors, sorted(cursors))

    def test_non_loopback_server_binding_remains_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_server(self.workspace, host="0.0.0.0", port=0)

    def test_all_observation_routes_open_no_product_write_transaction(self) -> None:
        _, receipt = self._capture(1)
        kernel_before = self._kernel_snapshot()
        audit_before = self.service.store.verify_audit()
        changes_before = self.service.store.connection.total_changes
        routes = (
            "/api/observations?limit=10",
            f"/api/observations/{receipt['trace_id']}",
            "/api/observations/stream?after=0",
            "/observations",
        )

        with patch.object(
            self.service.store,
            "transaction",
            side_effect=AssertionError("read route opened a write transaction"),
        ):
            for route in routes:
                with self.subTest(route=route):
                    status, _, _ = self._get(route)
                    self.assertEqual(200, status)

        self.assertEqual(kernel_before, self._kernel_snapshot())
        self.assertEqual(audit_before, self.service.store.verify_audit())
        self.assertEqual(changes_before, self.service.store.connection.total_changes)

    def test_observations_html_is_dependency_free_and_cursor_aware(self) -> None:
        status, content_type, body = self._get("/observations")

        self.assertEqual(200, status)
        self.assertEqual("text/html; charset=utf-8", content_type)
        assert isinstance(body, bytes)
        html = body.decode("utf-8")
        self.assertIn("Shared Mind Observations", html)
        self.assertIn("EventSource", html)
        self.assertIn("/api/observations/stream", html)
        self.assertIn("after=", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)


if __name__ == "__main__":
    unittest.main()
