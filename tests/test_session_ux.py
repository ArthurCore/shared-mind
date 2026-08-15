from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_mind.cli import EXIT_INTEGRITY_ERROR, EXIT_OK, build_parser, main
from shared_mind.product import ProductService
from shared_mind.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]


class SessionUxTest(unittest.TestCase):
    def test_uv_is_the_primary_install_without_manual_virtualenv_activation(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        mcp_guide = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
        bootstrap = (ROOT / "docs" / "agent-bootstrap.md").read_text(
            encoding="utf-8"
        )
        quick_start = readme.split("## Quick start", 1)[1].split("## Authority", 1)[0]

        install = "uv tool install --editable '.[mcp]'"
        self.assertIn(install, quick_start)
        self.assertIn(install, mcp_guide)
        self.assertNotIn("python3 -m venv", quick_start)
        self.assertNotIn("source .venv/bin/activate", quick_start)
        self.assertIn("$ shared-mind resume", quick_start)
        self.assertIn("$ shared-mind resume", bootstrap)

    def test_resume_parser_has_safe_task_aware_defaults(self) -> None:
        arguments = build_parser().parse_args(["resume"])

        self.assertEqual("resume", arguments.command)
        self.assertEqual(
            "Continue the highest-priority unblocked project work.", arguments.task
        )
        self.assertEqual("EVIDENCE", arguments.depth)
        self.assertEqual(128 * 1024, arguments.budget_bytes)

    def test_workspace_discovery_finds_the_project_sibling_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "atlas"
            nested = project / "src" / "package"
            nested.mkdir(parents=True)
            expected = Workspace.initialize(root / "atlas-memory", purpose="Atlas")

            discovered = Workspace.discover(nested)

        self.assertEqual(expected.root, discovered.root)

    def test_resume_is_one_command_from_the_project_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "atlas"
            nested = project / "src"
            nested.mkdir(parents=True)
            Workspace.initialize(root / "atlas-memory", purpose="Atlas continuity")
            output = io.StringIO()
            previous = Path.cwd()
            os.chdir(nested)
            try:
                exit_code = main(["resume"], stdout=output)
            finally:
                os.chdir(previous)

        self.assertEqual(EXIT_OK, exit_code, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual("SESSION_READY", result["code"])
        self.assertTrue(result["data"]["integrity"]["valid"])
        self.assertEqual(
            "Continue the highest-priority unblocked project work.",
            result["data"]["context"]["request"]["task"],
        )
        self.assertEqual("EVIDENCE", result["data"]["context"]["request"]["depth"])

    def test_resume_fails_closed_before_context_when_integrity_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace.initialize(Path(temporary) / "memory")
            output = io.StringIO()
            with (
                patch.object(ProductService, "verify", return_value={"valid": False}),
                patch.object(ProductService, "context") as context,
            ):
                exit_code = main(
                    ["--workspace", str(workspace.root), "resume"], stdout=output
                )

        self.assertEqual(EXIT_INTEGRITY_ERROR, exit_code)
        self.assertEqual("PRODUCT_INTEGRITY_INVALID", json.loads(output.getvalue())["code"])
        context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
