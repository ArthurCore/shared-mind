from __future__ import annotations

import inspect
import sqlite3
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_mind.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]


class ProductMcpAdapterBindingTest(unittest.TestCase):
    """Cover the SDK binding layer of the product adapter.

    The pure ProductMcpApplication logic is exercised elsewhere. These tests
    target `create_server`, which is where SDK registration rules apply and
    where a startup failure would otherwise reach users unnoticed.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace.initialize(
            Path(self.temp.name) / "workspace",
            purpose="Exercise the product MCP binding surface.",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_resource_handlers_declare_no_uri_template_parameters(self) -> None:
        """Static resource URIs must bind without handler parameters.

        SDK v2 derives URI template variables from the handler signature, so a
        default-argument capture such as ``def resource(uri: str = uri)`` makes
        registration fail for every static URI.
        """

        server = self.build_server()
        self.assertTrue(server.resources, "no product resources were registered")
        for uri, (_, handler) in server.resources.items():
            with self.subTest(uri=uri):
                parameters = inspect.signature(handler).parameters
                self.assertEqual(
                    {},
                    dict(parameters),
                    f"resource handler for {uri} must take no parameters",
                )

    def test_each_resource_handler_returns_its_own_uri_content(self) -> None:
        """Loop-bound closures must not collapse onto the last URI."""

        server = self.build_server()
        seen: dict[str, str] = {}
        for uri, (_, handler) in server.resources.items():
            seen[uri] = handler()
        self.assertEqual(
            len(seen), len(set(seen.values())), "resource handlers returned shared text"
        )

    def build_server(self):
        from shared_mind import product_mcp_server

        types_module = types.ModuleType("mcp.types")
        types_module.ToolAnnotations = _RecordingToolAnnotations
        server_module = types.ModuleType("mcp.server")
        server_module.MCPServer = _RecordingServer
        fastmcp_module = types.ModuleType("mcp.server.fastmcp")
        fastmcp_module.FastMCP = _RecordingServer
        modules = {
            "mcp.types": types_module,
            "mcp.server": server_module,
            "mcp.server.fastmcp": fastmcp_module,
        }
        with patch.dict("sys.modules", modules):
            return product_mcp_server.create_server(self.workspace)


class ProductStoreThreadAffinityTest(unittest.TestCase):
    """The product store is reached from SDK worker threads.

    MCP runs synchronous handlers through ``anyio.to_thread.run_sync``, so a
    connection pinned to its creating thread makes every database-backed
    resource and tool fail at read time even after registration succeeds.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace.initialize(
            Path(self.temp.name) / "workspace",
            purpose="Exercise product store thread affinity.",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_store_is_readable_from_another_thread(self) -> None:
        from shared_mind.product import ProductService

        service = ProductService(self.workspace)
        self.addCleanup(service.close)

        outcome: dict[str, object] = {}

        def read() -> None:
            try:
                outcome["value"] = service.describe()
            except sqlite3.ProgrammingError as error:  # pragma: no cover - failure path
                outcome["error"] = str(error)

        worker = threading.Thread(target=read)
        worker.start()
        worker.join()

        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertIn("value", outcome)


class _RecordingToolAnnotations:
    def __init__(self, **metadata: object) -> None:
        self.metadata = metadata


class _RecordingServer:
    def __init__(self, name: str, instructions: str | None = None, **_: object) -> None:
        self.name = name
        self.instructions = instructions or ""
        self.tools: dict[str, tuple[dict[str, object], object]] = {}
        self.resources: dict[str, tuple[dict[str, object], object]] = {}

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

    def run(self, *, transport: str) -> None:  # pragma: no cover - not exercised
        raise AssertionError("run must not be called during registration tests")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
