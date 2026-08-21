from __future__ import annotations

import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_mind.cli import (
    CliUsageError,
    DEFAULT_RESUME_BUDGET_BYTES,
    EXIT_INTEGRITY_ERROR,
    EXIT_OK,
    build_parser,
    main,
)
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
        self.assertIn("$ shared-mind setup --install-hooks", quick_start)
        self.assertIn("$ shared-mind setup --install-hooks", bootstrap)
        self.assertIn("$ shared-mind session start", quick_start)
        self.assertIn("$ shared-mind session start", bootstrap)
        self.assertIn("Manual resume remains", bootstrap)

    def test_resume_parser_has_safe_task_aware_defaults(self) -> None:
        arguments = build_parser().parse_args(["resume"])

        self.assertEqual("resume", arguments.command)
        self.assertEqual(
            "Continue the highest-priority unblocked project work.", arguments.task
        )
        self.assertEqual("EVIDENCE", arguments.depth)
        self.assertEqual(24 * 1024, DEFAULT_RESUME_BUDGET_BYTES)
        self.assertEqual(DEFAULT_RESUME_BUDGET_BYTES, arguments.budget_bytes)

    def test_resume_preserves_an_explicit_128_kib_evidence_budget(self) -> None:
        arguments = build_parser().parse_args(
            ["resume", "--budget-bytes", str(128 * 1024)]
        )

        self.assertEqual(128 * 1024, arguments.budget_bytes)

    def test_resume_rejects_a_budget_above_the_128_kib_safety_ceiling(self) -> None:
        with self.assertRaises(CliUsageError):
            build_parser().parse_args(
                ["resume", "--budget-bytes", str((128 * 1024) + 1)]
            )

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

    def test_default_resume_restores_compact_continuity_and_drill_down_refs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace.initialize(
                Path(temporary) / "atlas-memory", purpose="Atlas continuity"
            )
            service = ProductService(workspace)
            try:
                source = workspace.root / "continuity.md"
                source.write_text(
                    "\n".join(
                        (
                            "FACT: system:atlas | deployment.database_engine@1 | software:postgresql | production",
                            "DECISION: Keep PostgreSQL | Continue with PostgreSQL | Verified runbook | Migrate later",
                            "QUESTION: Which maintenance window? | Not yet approved",
                            "WORK: P0 | Validate migration runbook",
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                conflicting = workspace.root / "conflicting.md"
                conflicting.write_text(
                    "FACT: system:atlas | deployment.database_engine@1 | software:mysql | production\n",
                    encoding="utf-8",
                )
                for path in (source, conflicting):
                    batch = service.ingest([path])
                    service.extract(batch["batch_id"])
                    service.commit_batch_drafts(batch["batch_id"])
                service.build_memory_views()
            finally:
                service.close()

            contexts = []
            for _ in range(2):
                output = io.StringIO()
                exit_code = main(
                    ["--workspace", str(workspace.root), "resume"], stdout=output
                )
                self.assertEqual(EXIT_OK, exit_code, output.getvalue())
                contexts.append(json.loads(output.getvalue())["data"]["context"])

        first, second = contexts
        self.assertEqual(first, second)
        self.assertEqual(24 * 1024, first["budget"]["budget_bytes"])
        self.assertLessEqual(first["budget"]["included_bytes"], 24 * 1024)
        self.assertLess(first["budget"]["included_bytes"], (128 * 1024) // 4)
        self.assertEqual(
            first["budget"]["included_bytes"],
            len(
                json.dumps(
                    first,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        )
        core = first["core_context"]
        self.assertEqual("Atlas continuity", core["purpose"])
        self.assertEqual(1, len(core["decisions"]))
        self.assertEqual(1, len(core["open_questions"]))
        self.assertEqual(1, len(core["open_conflicts"]))
        self.assertEqual(1, len(core["work_items"]))
        for section in ("decisions", "open_questions", "open_conflicts", "work_items"):
            self.assertTrue(core[section][0]["projection_ref"])
        truncation = core["truncation"]
        self.assertEqual(
            math.ceil(truncation["rendered_bytes"] / 4),
            truncation["estimated_tokens"],
        )

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
