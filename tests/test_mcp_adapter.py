from __future__ import annotations

import asyncio
import copy
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from shared_mind.canonical import canonical_json
from shared_mind.service import WorkspaceService
from shared_mind.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = (
    "context",
    "query",
    "proposal_validate",
    "proposal_commit",
    "source_add",
    "conflict_list",
    "ledger_verify",
)
RESOURCE_URIS = (
    "shared-mind://workspace/info",
    "shared-mind://workspace/context",
    "shared-mind://projection/project.json",
    "shared-mind://projection/project.md",
    "shared-mind://contract/schema",
    "shared-mind://contract/predicate-registry",
)


class McpAdapterContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temp.name) / "workspace"
        self.workspace = Workspace.initialize(
            self.workspace_root,
            purpose="Exercise the local MCP adapter contract.",
        )
        fixtures = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.objects = {
            item["name"]: item["object"] for item in fixtures["typed_objects"]
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_core_import_does_not_require_the_optional_mcp_sdk(self) -> None:
        script = """
import builtins
real_import = builtins.__import__
def without_mcp(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'mcp' or name.startswith('mcp.'):
        raise ModuleNotFoundError("optional MCP SDK intentionally unavailable")
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = without_mcp
import shared_mind
from shared_mind.mcp_server import McpApplication
print(shared_mind.__name__, McpApplication.__name__)
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("shared_mind McpApplication\n", completed.stdout)

    def test_core_dispatcher_has_exact_tool_catalog_and_closed_schemas(self) -> None:
        application = self.application()

        tools = application.list_tools()

        self.assertEqual(TOOL_NAMES, tuple(tool["name"] for tool in tools))
        self.assertEqual(len(TOOL_NAMES), len({tool["name"] for tool in tools}))
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"])
                input_schema = tool["inputSchema"]
                self.assertEqual("object", input_schema["type"])
                self.assertIs(input_schema["additionalProperties"], False)
                output_schema = tool["outputSchema"]
                self.assertEqual("object", output_schema["type"])
                self.assertIn("ok", output_schema["properties"])
                self.assertIn("code", output_schema["properties"])
                self.assertEqual(["ok", "code"], output_schema["required"])

        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(
            ["proposal"], by_name["proposal_validate"]["inputSchema"]["required"]
        )
        self.assertEqual(
            ["proposal"], by_name["proposal_commit"]["inputSchema"]["required"]
        )
        self.assertEqual(["path"], by_name["source_add"]["inputSchema"]["required"])
        self.assertEqual(
            {"OPEN", "RESOLVED", "REOPENED"},
            set(
                by_name["conflict_list"]["inputSchema"]["properties"]["status"][
                    "enum"
                ]
            ),
        )
        self.assertEqual(
            {"kinds", "ids", "title_contains", "predicates", "source_ids",
             "source_revision_ids", "statuses", "limit", "offset", "summary_only"},
            set(by_name["query"]["inputSchema"]["properties"]),
        )

    def test_validate_and_query_calls_match_workspace_service_envelopes(self) -> None:
        application = self.application()
        service = WorkspaceService(self.workspace)
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])

        valid = application.call_tool("proposal_validate", {"proposal": proposal})
        expected_valid = service.validate_proposal(proposal).as_dict()
        queried = application.call_tool("query", {})
        expected_query = service.query({}).as_dict()
        invalid = application.call_tool("proposal_validate", {"proposal": {}})
        expected_invalid = service.validate_proposal({}).as_dict()

        self.assert_call(valid, expected_valid, is_error=False)
        self.assert_call(queried, expected_query, is_error=False)
        self.assert_call(invalid, expected_invalid, is_error=True)
        missing = application.call_tool("not_a_tool", {})
        self.assertTrue(missing["isError"])
        self.assertEqual("MCP_TOOL_NOT_FOUND", missing["structuredContent"]["code"])

    def test_fact_conflict_remains_a_successful_proposal_commit(self) -> None:
        application = self.application()
        self.seed_source_and_claim()
        conflicting = copy.deepcopy(
            self.objects["assert_mysql_same_interval_proposal"]
        )

        result = application.call_tool(
            "proposal_commit", {"proposal": conflicting}
        )

        self.assertFalse(result["isError"])
        self.assertEqual("FACT_CONFLICT", result["structuredContent"]["code"])
        self.assertTrue(result["structuredContent"]["ok"])
        self.assertEqual(
            "FACT_CONFLICT",
            result["structuredContent"]["data"]["decision_receipt"]["outcome"],
        )

    def test_source_add_is_relative_to_source_root_and_blocks_path_escape(self) -> None:
        application = self.application()
        source = self.workspace.source_root / "notes.md"
        source.write_text("# Local notes\n", encoding="utf-8")
        outside = self.workspace_root / "outside.md"
        outside.write_text("must not be exposed\n", encoding="utf-8")

        accepted = application.call_tool(
            "source_add", {"path": "notes.md", "source_id": "document:mcp-notes"}
        )

        self.assertFalse(accepted["isError"])
        self.assertEqual("SOURCE_REGISTERED", accepted["structuredContent"]["code"])
        for unsafe_path in ("../outside.md", str(outside)):
            with self.subTest(path=unsafe_path):
                rejected = application.call_tool("source_add", {"path": unsafe_path})
                self.assertTrue(rejected["isError"])
                self.assertFalse(rejected["structuredContent"]["ok"])
                self.assertNotEqual(
                    "SOURCE_REGISTERED", rejected["structuredContent"]["code"]
                )
                self.assertNotIn("must not be exposed", canonical_json(rejected))

        if hasattr(os, "symlink"):
            symlink = self.workspace.source_root / "escape.md"
            try:
                symlink.symlink_to(outside)
            except OSError:
                pass
            else:
                rejected = application.call_tool("source_add", {"path": "escape.md"})
                self.assertTrue(rejected["isError"])
                self.assertNotEqual(
                    "SOURCE_REGISTERED", rejected["structuredContent"]["code"]
                )

    def test_read_only_tools_are_operation_results_and_do_not_advance_ledger(self) -> None:
        application = self.application()
        before = self.ledger_count()

        context = application.call_tool("context", {"budget_bytes": 32_000})
        conflicts = application.call_tool("conflict_list", {})
        verification = application.call_tool("ledger_verify", {})

        self.assertEqual("CONTEXT_READY", context["structuredContent"]["code"])
        self.assertEqual("CONFLICTS_LISTED", conflicts["structuredContent"]["code"])
        self.assertEqual("LEDGER_VALID", verification["structuredContent"]["code"])
        self.assertFalse(context["isError"])
        self.assertFalse(conflicts["isError"])
        self.assertFalse(verification["isError"])
        self.assertEqual(before, self.ledger_count())

    def test_resources_are_a_fixed_allowlist_without_file_or_sql_escape(self) -> None:
        application = self.application()

        resources = application.list_resources()

        self.assertEqual(RESOURCE_URIS, tuple(item["uri"] for item in resources))
        self.assertEqual(len(RESOURCE_URIS), len({item["uri"] for item in resources}))
        for descriptor in resources:
            with self.subTest(uri=descriptor["uri"]):
                self.assertTrue(descriptor["name"])
                self.assertTrue(descriptor["description"])
                self.assertIn(descriptor["mimeType"], {"application/json", "text/markdown"})
                result = application.read_resource(descriptor["uri"])
                self.assertEqual(1, len(result["contents"]))
                content = result["contents"][0]
                self.assertEqual(descriptor["uri"], content["uri"])
                self.assertEqual(descriptor["mimeType"], content["mimeType"])
                self.assertIsInstance(content["text"], str)
                self.assertTrue(content["text"])
                if descriptor["mimeType"] == "application/json":
                    self.assertIsNotNone(json.loads(content["text"]))

        forbidden = (
            "file:///etc/passwd",
            "sqlite:///tmp/shared-mind.sqlite3",
            "shared-mind://workspace/.shared-mind/shared-mind.sqlite3",
            "shared-mind://workspace/info?path=../../etc/passwd",
        )
        for uri in forbidden:
            with self.subTest(uri=uri):
                with self.assertRaises((KeyError, ValueError)):
                    application.read_resource(uri)

    def test_create_server_registers_the_same_surface_through_a_fake_sdk(self) -> None:
        module = self.module()

        with patch("mcp.server.fastmcp.FastMCP", RecordingFastMCP):
            server = module.create_server(self.workspace)

        self.assertEqual("shared-mind", server.name)
        self.assertIn("local", server.instructions.lower())
        self.assertEqual(TOOL_NAMES, tuple(server.tools))
        self.assertEqual(RESOURCE_URIS, tuple(server.resources))
        for metadata, _ in server.tools.values():
            self.assertIs(metadata["structured_output"], True)
            self.assertTrue(metadata["description"])

        function = server.tools["proposal_validate"][1]
        returned = function(
            proposal=copy.deepcopy(self.objects["assert_postgresql_proposal"])
        )
        if inspect.isawaitable(returned):
            returned = asyncio.run(returned)
        self.assertEqual("PROPOSAL_VALID", returned["code"])
        self.assertTrue(returned["ok"])

    def test_installed_sdk_exposes_transport_metadata_and_structured_output(self) -> None:
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError:
            self.skipTest("optional MCP SDK is not installed")
        module = self.module()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            server = module.create_server(self.workspace)
            tools = asyncio.run(server.list_tools())
            resources = asyncio.run(server.list_resources())
            result = asyncio.run(
                server.call_tool(
                    "proposal_validate",
                    {
                        "proposal": copy.deepcopy(
                            self.objects["assert_postgresql_proposal"]
                        )
                    },
                )
            )

        self.assertIsInstance(server, FastMCP)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(TOOL_NAMES, tuple(tool.name for tool in tools))
        self.assertEqual(RESOURCE_URIS, tuple(str(resource.uri) for resource in resources))
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertEqual("object", tool.inputSchema["type"])
                self.assertIsNotNone(tool.outputSchema)
                self.assertEqual("object", tool.outputSchema["type"])
                self.assertIn("ok", tool.outputSchema.get("properties", {}))
                self.assertIn("code", tool.outputSchema.get("properties", {}))
        content, structured = result
        self.assertEqual("PROPOSAL_VALID", structured["code"])
        self.assertTrue(structured["ok"])
        self.assertTrue(content)

    def test_main_runs_stdio_without_writing_protocol_noise_to_stdout(self) -> None:
        module = self.module()
        server = RecordingFastMCP("shared-mind")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(module, "create_server", return_value=server):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = module.main(
                    ["--workspace", str(self.workspace_root)]
                )

        self.assertEqual(0, exit_code)
        self.assertEqual([("stdio", None)], server.run_calls)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def application(self):
        return self.module().McpApplication(self.workspace)

    @staticmethod
    def module():
        from shared_mind import mcp_server

        return mcp_server

    def assert_call(
        self, result: dict[str, object], envelope: dict[str, object], *, is_error: bool
    ) -> None:
        self.assertEqual({"content", "structuredContent", "isError"}, set(result))
        self.assertEqual(envelope, result["structuredContent"])
        self.assertIs(is_error, result["isError"])
        self.assertEqual(
            [{"type": "text", "text": canonical_json(envelope)}], result["content"]
        )

    def seed_source_and_claim(self) -> None:
        content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        kernel = self.workspace.open_kernel()
        try:
            source_receipt = kernel.register_source(
                copy.deepcopy(self.objects["source_revision_postgresql"]), content
            )
        finally:
            kernel.close()
        self.assertEqual("COMMITTED", source_receipt.outcome)
        result = WorkspaceService(self.workspace).commit_proposal(
            copy.deepcopy(self.objects["assert_postgresql_proposal"])
        )
        self.assertEqual("COMMITTED", result.code)

    def ledger_count(self) -> int:
        kernel = self.workspace.open_kernel()
        try:
            return int(kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
        finally:
            kernel.close()


class RecordingFastMCP:
    def __init__(self, name: str, instructions: str | None = None, **_: object) -> None:
        self.name = name
        self.instructions = instructions or ""
        self.tools: dict[str, tuple[dict[str, object], object]] = {}
        self.resources: dict[str, tuple[dict[str, object], object]] = {}
        self.run_calls: list[tuple[str, str | None]] = []

    def tool(self, *, name: str, **metadata: object):
        def register(function):
            self.tools[name] = (metadata, function)
            return function

        return register

    def resource(self, uri: str, **metadata: object):
        def register(function):
            self.resources[uri] = (metadata, function)
            return function

        return register

    def run(self, transport: str = "stdio", mount_path: str | None = None) -> None:
        self.run_calls.append((transport, mount_path))


if __name__ == "__main__":
    unittest.main()
