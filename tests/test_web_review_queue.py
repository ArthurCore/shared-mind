from __future__ import annotations

import io
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from shared_mind.product_cli import main as product_cli_main
from shared_mind.web_control import WebControlApplication, create_server

from tests.product_support import ProductTestCase


CSRF_HEADER = "X-Shared-Mind-CSRF-Token"


class WebReviewQueueTest(ProductTestCase):
    def _draft(self, index: int = 1) -> dict:
        source = self.write_source(
            f"dev-104-{index}.md",
            f"WORK: P{index % 3} | Review DEV-104 candidate {index}\n",
        )
        batch = self.service.ingest([source])
        extraction = self.service.extract(batch["batch_id"])
        drafts = self.service.list_drafts(batch_id=batch["batch_id"])
        return next(item for item in drafts if item["draft_kind"] == "KERNEL_PROPOSAL")

    def _kernel_snapshot(self) -> dict[str, int | str | None]:
        kernel = self.workspace.open_kernel()
        try:
            last = kernel.connection.execute(
                "SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            return {
                "ledger_count": kernel.connection.execute(
                    "SELECT COUNT(*) FROM ledger"
                ).fetchone()[0],
                "receipt_count": kernel.connection.execute(
                    "SELECT COUNT(*) FROM receipts"
                ).fetchone()[0],
                "state_root": kernel.state_root(),
                "head_seq": int(last["seq"]) if last else 0,
                "head_hash": str(last["entry_hash"]) if last else None,
            }
        finally:
            kernel.close()

    def _token(self, app: WebControlApplication) -> str:
        token = getattr(app, "csrf_token", None)
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 32)
        return token

    def _post(
        self,
        app: WebControlApplication,
        path: str,
        body: dict,
        *,
        token: str | None,
    ) -> tuple[int, dict]:
        headers = {} if token is None else {CSRF_HEADER: token}
        status, _, response = app.handle(
            "POST", path, json.dumps(body).encode("utf-8"), headers=headers
        )
        return status, json.loads(response)

    def _product_cli(self, *arguments: str) -> tuple[int, dict]:
        output = io.StringIO()
        code = product_cli_main(
            ["--workspace", str(self.workspace_root), *arguments], stdout=output
        )
        return code, json.loads(output.getvalue())

    def test_web_commit_and_cli_commit_return_identical_receipt_and_audit_outcome(
        self,
    ) -> None:
        draft = self._draft()
        app = WebControlApplication(self.service)
        with patch.object(
            self.service, "commit_draft", wraps=self.service.commit_draft
        ) as boundary:
            web_status, web = self._post(
                app,
                f"/api/drafts/{draft['draft_id']}/commit",
                {"draft_id": draft["draft_id"]},
                token=self._token(app),
            )
        self.assertEqual(200, web_status, web)
        boundary.assert_called_once_with(draft["draft_id"])
        kernel_after_web = self._kernel_snapshot()
        audit_after_web = self.service.store.verify_audit()

        cli_code, cli = self._product_cli("draft", "commit", draft["draft_id"])

        self.assertEqual(0, cli_code, cli)
        self.assertEqual(web["data"], cli["data"])
        self.assertEqual(web["data"]["receipt"], cli["data"]["receipt"])
        self.assertEqual(kernel_after_web, self._kernel_snapshot())
        self.assertEqual(audit_after_web, self.service.store.verify_audit())

    def test_web_reject_changes_no_kernel_ledger_head_or_state_root(self) -> None:
        draft = self._draft()
        app = WebControlApplication(self.service)
        before = self._kernel_snapshot()

        status, response = self._post(
            app,
            f"/api/drafts/{draft['draft_id']}/reject",
            {"draft_id": draft["draft_id"], "rationale": "Evidence is incomplete."},
            token=self._token(app),
        )

        self.assertEqual(200, status, response)
        self.assertEqual("REJECTED", response["data"]["status"])
        self.assertEqual(before, self._kernel_snapshot())

    def test_web_recommit_is_idempotent_without_new_ledger_or_product_audit(self) -> None:
        draft = self._draft()
        app = WebControlApplication(self.service)
        body = {"draft_id": draft["draft_id"]}
        first_status, first = self._post(
            app,
            f"/api/drafts/{draft['draft_id']}/commit",
            body,
            token=self._token(app),
        )
        self.assertEqual(200, first_status, first)
        kernel_before = self._kernel_snapshot()
        audit_before = self.service.store.verify_audit()

        second_status, second = self._post(
            app,
            f"/api/drafts/{draft['draft_id']}/commit",
            body,
            token=self._token(app),
        )

        self.assertEqual(200, second_status, second)
        self.assertEqual(first["data"], second["data"])
        self.assertEqual(kernel_before, self._kernel_snapshot())
        self.assertEqual(audit_before, self.service.store.verify_audit())

    def test_validation_failure_is_fail_closed_with_zero_canonical_mutation(self) -> None:
        draft = self._draft()
        with self.service.store.transaction():
            invalid = self.service.store.update_draft(
                draft["draft_id"], document={}, expected_version=draft["version"]
            )
        app = WebControlApplication(self.service)
        before = self._kernel_snapshot()

        status, response = self._post(
            app,
            f"/api/drafts/{draft['draft_id']}/commit",
            {"draft_id": draft["draft_id"]},
            token=self._token(app),
        )

        self.assertEqual(400, status, response)
        self.assertEqual("DRAFT_COMMIT_FAILED", response["code"])
        self.assertEqual(before, self._kernel_snapshot())
        self.assertEqual("FAILED", self.service.get_draft(invalid["draft_id"])["status"])

    def test_state_changing_posts_require_ephemeral_csrf_before_service_mutation(
        self,
    ) -> None:
        draft = self._draft()
        app = WebControlApplication(self.service)
        other = WebControlApplication(self.service)
        token = self._token(app)
        self.assertNotEqual(token, self._token(other))
        kernel_before = self._kernel_snapshot()
        audit_before = self.service.store.verify_audit()
        requests = (
            (
                f"/api/drafts/{draft['draft_id']}/commit",
                {"draft_id": draft["draft_id"]},
            ),
            (
                f"/api/drafts/{draft['draft_id']}/reject",
                {"draft_id": draft["draft_id"], "rationale": "reject"},
            ),
            ("/api/build", {"target": "all"}),
            ("/api/context", {}),
            ("/api/search", {"query": "DEV-104"}),
            ("/api/tool", {"name": "capabilities", "arguments": {}}),
        )
        with ExitStack() as stack:
            for name in (
                "commit_draft",
                "reject_draft",
                "build_memory_views",
                "build_indexes",
                "context",
                "search",
                "tool_call",
            ):
                stack.enter_context(
                    patch.object(
                        self.service,
                        name,
                        side_effect=AssertionError("mutation called before CSRF"),
                    )
                )
            for path, body in requests:
                for supplied in (None, "wrong-token"):
                    with self.subTest(path=path, supplied=supplied):
                        status, response = self._post(
                            app, path, body, token=supplied
                        )
                        self.assertEqual(403, status, response)
                        self.assertEqual("CSRF_TOKEN_INVALID", response["code"])

        self.assertEqual(kernel_before, self._kernel_snapshot())
        self.assertEqual(audit_before, self.service.store.verify_audit())
        with self.assertRaises(ValueError):
            create_server(self.workspace, host="0.0.0.0", port=0)

    def test_draft_state_detail_provenance_and_review_page_have_no_bulk_path(self) -> None:
        draft = self._draft()
        app = WebControlApplication(self.service)
        audit_before = self.service.store.verify_audit()

        list_status, _, list_body = app.handle("GET", "/api/drafts?state=DRAFT")
        detail_status, _, detail_body = app.handle(
            "GET", f"/api/drafts/{draft['draft_id']}"
        )
        review_status, review_type, review_body = app.handle("GET", "/review")

        listed = json.loads(list_body)
        detail = json.loads(detail_body)
        self.assertEqual(200, list_status, listed)
        self.assertEqual([draft["draft_id"]], [item["draft_id"] for item in listed["data"]["drafts"]])
        self.assertEqual(200, detail_status, detail)
        provenance = detail["data"]["provenance"]
        for field in (
            "extractor_id",
            "extractor_version",
            "model",
            "prompt_version",
            "input_revision_ids",
        ):
            self.assertIn(field, provenance)
        self.assertEqual(200, review_status)
        self.assertEqual("text/html; charset=utf-8", review_type)
        html = review_body.decode("utf-8")
        self.assertIn("Shared Mind Review Queue", html)
        self.assertIn(self._token(app), html)
        self.assertIn("/api/drafts?state=", html)
        self.assertIn("/commit", html)
        self.assertIn("/reject", html)
        self.assertIn("provenance", html)
        self.assertNotIn("commit-batch", html)
        self.assertNotIn("approve all", html.lower())
        self.assertNotIn("<script src=", html)
        self.assertNotIn("https://", html)
        self.assertEqual(audit_before, self.service.store.verify_audit())


if __name__ == "__main__":
    unittest.main()
