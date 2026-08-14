from __future__ import annotations

import io
import json
import unittest

from shared_mind import cli as kernel_cli
from shared_mind.product_cli import main as product_cli_main
from shared_mind.skills import build_skill_record, create_skill
from shared_mind.product_mcp_server import ProductMcpApplication, RESOURCE_URIS, TOOL_NAMES
from shared_mind.web_control import WebControlApplication, create_server

from tests.product_support import ProductTestCase


class ProductInterfacesTest(ProductTestCase):
    def _product_cli(self, *args: str):
        output = io.StringIO()
        code = product_cli_main(
            ["--workspace", str(self.workspace_root), *args], stdout=output
        )
        return code, json.loads(output.getvalue())

    def _kernel_cli(self, *args: str):
        output = io.StringIO()
        code = kernel_cli.main(
            ["--workspace", str(self.workspace_root), *args], stdout=output
        )
        return code, json.loads(output.getvalue())

    def test_product_cli_review_build_context_and_verify(self) -> None:
        source = self.write_source()
        code, ingest = self._product_cli("ingest", source.name)
        self.assertEqual(0, code)
        batch_id = ingest["data"]["batch_id"]
        code, extracted = self._product_cli("extract", batch_id)
        self.assertEqual(0, code)
        self.assertEqual(2, extracted["data"]["created"])
        code, drafts = self._product_cli("draft", "list", "--batch-id", batch_id)
        self.assertEqual(2, drafts["data"]["count"])
        code, committed = self._product_cli("draft", "commit-batch", batch_id)
        self.assertEqual([], committed["data"]["failed"])
        code, built = self._product_cli("build", "all")
        self.assertIn("views", built["data"])
        code, context = self._product_cli(
            "context",
            "--task",
            "Review PostgreSQL migration",
            "--query",
            "postgresql",
            "--budget-bytes",
            "8192",
        )
        self.assertEqual(0, code)
        self.assertIn("context_hash", context["data"])
        code, verified = self._product_cli("verify")
        self.assertEqual(0, code)
        self.assertTrue(verified["data"]["valid"])

    def test_skill_review_lifecycle_is_exposed_by_cli_mcp_and_web(self) -> None:
        skill = build_skill_record(
            skill_id="skill:interface-review",
            version=1,
            purpose="Review interface changes",
            triggers=["interface review"],
            steps=["write review"],
            validation_rules=[{"type": "NON_EMPTY"}],
            provenance={"test": True},
        )
        create_skill(self.service.store, skill)
        evidence = json.dumps({"passed": True, "runner": "fixture"})
        code, tested = self._product_cli(
            "skill",
            "mark-tested",
            skill["skill_id"],
            "1",
            "--evidence",
            evidence,
        )
        self.assertEqual(0, code)
        self.assertEqual("TESTED", tested["data"]["status"])

        app = ProductMcpApplication(self.workspace)
        try:
            approved = app.call_tool(
                "skill_approve",
                {
                    "skill_id": skill["skill_id"],
                    "version": 1,
                    "approval": {"by": "human:test"},
                },
            )
            self.assertFalse(approved["isError"])
            self.assertEqual(
                "APPROVED", approved["structuredContent"]["data"]["status"]
            )
        finally:
            app.close()

        second = build_skill_record(
            skill_id="skill:web-review",
            version=1,
            purpose="Review web changes",
            triggers=["web review"],
            steps=["write review"],
            validation_rules=[{"type": "NON_EMPTY"}],
            provenance={"test": True},
        )
        create_skill(self.service.store, second)
        web = WebControlApplication(self.service)
        status, _, body = web.handle(
            "POST",
            "/api/skills/skill:web-review/1/mark-tested",
            json.dumps({"evidence": {"passed": True}}).encode(),
        )
        self.assertEqual(200, status)
        self.assertEqual("TESTED", json.loads(body)["data"]["status"])

    def test_existing_shared_mind_context_supports_task_aware_mode(self) -> None:
        self.seed_product()
        code, response = self._kernel_cli(
            "context",
            "--task",
            "Review PostgreSQL migration",
            "--query",
            "postgresql",
            "--budget-bytes",
            "8192",
        )
        self.assertEqual(0, code)
        self.assertEqual("TASK_CONTEXT_READY", response["code"])
        self.assertIn("context_hash", response["data"]["context"])

    def test_product_mcp_review_context_and_path_sandbox(self) -> None:
        source = self.write_source()
        app = ProductMcpApplication(self.workspace)
        try:
            self.assertEqual(set(TOOL_NAMES), {item["name"] for item in app.list_tools()})
            ingest = app.call_tool("ingest", {"paths": [source.name]})
            self.assertFalse(ingest["isError"])
            batch_id = ingest["structuredContent"]["data"]["batch_id"]
            extracted = app.call_tool("extract", {"batch_id": batch_id})
            draft_id = extracted["structuredContent"]["data"]["draft_ids"][0]
            shown = app.call_tool("draft_show", {"draft_id": draft_id})
            self.assertFalse(shown["isError"])
            committed = app.call_tool("draft_commit", {"draft_id": draft_id})
            self.assertFalse(committed["isError"])
            app.call_tool("product_build", {"target": "all"})
            context = app.call_tool(
                "task_context", {"task": "continue implementation", "budget_bytes": 8192}
            )
            self.assertFalse(context["isError"])
            capabilities = app.call_tool(
                "memory_tool", {"name": "capabilities", "arguments": {}}
            )
            self.assertFalse(capabilities["isError"])
            self.assertIn(
                "read_source_span",
                capabilities["structuredContent"]["data"]["tools"],
            )
            escaped = app.call_tool("ingest", {"paths": ["../secret.md"]})
            self.assertTrue(escaped["isError"])
            self.assertEqual("PATH_OUTSIDE_WORKSPACE", escaped["structuredContent"]["code"])
            self.assertEqual(set(RESOURCE_URIS), {item["uri"] for item in app.list_resources()})
            catalog = app.read_resource("shared-mind-product://catalog")
            self.assertIn("items", json.loads(catalog["contents"][0]["text"]))
        finally:
            app.close()

    def test_web_application_uses_service_boundary(self) -> None:
        self.seed_product()
        app = WebControlApplication(self.service)
        status, content_type, body = app.handle("GET", "/api/catalog")
        self.assertEqual(200, status)
        self.assertTrue(content_type.startswith("application/json"))
        self.assertTrue(json.loads(body)["ok"])
        status, _, body = app.handle(
            "POST",
            "/api/context",
            json.dumps(
                {
                    "task": "Review migration",
                    "purpose": None,
                    "query": "postgresql",
                    "references": [],
                    "depth": "DETAIL",
                    "budget_bytes": 8192,
                    "budget_tokens": None,
                    "hints": {},
                }
            ).encode(),
        )
        self.assertEqual(200, status)
        self.assertIn("context_hash", json.loads(body)["data"])
        status, _, body = app.handle(
            "POST",
            "/api/tool",
            json.dumps({"name": "capabilities", "arguments": {}}).encode(),
        )
        self.assertEqual(200, status)
        self.assertIn("read_source_span", json.loads(body)["data"]["tools"])
        status, _, body = app.handle("GET", "/api/missing")
        self.assertEqual(404, status)
        self.assertEqual("ROUTE_NOT_FOUND", json.loads(body)["code"])

    def test_web_server_rejects_non_loopback_binding(self) -> None:
        with self.assertRaises(ValueError):
            create_server(self.workspace, host="0.0.0.0", port=0)
        server = create_server(self.workspace, host="127.0.0.1", port=0)
        try:
            self.assertTrue(server.server_address[0] in {"127.0.0.1", "::1"})
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
