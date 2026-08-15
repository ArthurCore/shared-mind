from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shared_mind.cli import EXIT_OK, build_parser, main


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
BOOTSTRAP = ROOT / "docs" / "agent-bootstrap.md"


class AgentBootstrapDocumentationTest(unittest.TestCase):
    def test_readme_links_to_the_agent_bootstrap_and_every_local_link_exists(self) -> None:
        readme = README.read_text(encoding="utf-8")

        self.assertIn("[Coding-agent bootstrap](docs/agent-bootstrap.md)", readme)
        self.assertTrue(BOOTSTRAP.is_file())
        for document in (README, BOOTSTRAP):
            text = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
                if "://" in target or target.startswith("#"):
                    continue
                path = target.split("#", 1)[0]
                if path:
                    self.assertTrue(
                        (document.parent / path).is_file(),
                        f"broken link in {document}: {target}",
                    )

    def test_every_documented_shared_mind_command_conforms_to_the_real_parser(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        commands = [
            line.removeprefix("$ shared-mind ")
            for line in text.splitlines()
            if line.startswith("$ shared-mind ")
        ]

        self.assertEqual(
            {
                "init",
                "source",
                "proposal",
                "context",
                "resume",
                "conflict",
                "replay",
                "project",
            },
            {build_parser().parse_args(shlex.split(command)).command for command in commands},
        )
        self.assertIn("resume", commands)
        self.assertIn("proposal validate proposal.json", commands)
        self.assertIn("proposal commit proposal.json --json", commands)
        self.assertIn("project --format markdown", commands)
        self.assertIn("replay --verify", commands)

    def test_one_documented_command_bootstraps_context_from_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "memory"
            stdout = io.StringIO()
            self.assertEqual(EXIT_OK, main(["init", str(workspace)], stdout=stdout))
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "src")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "shared_mind",
                    "context",
                    "--budget-tokens",
                    "4096",
                ],
                cwd=workspace,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual("", completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(1, len(lines), completed.stdout)
        result = json.loads(lines[0])
        self.assertEqual("CONTEXT_READY", result["code"])
        context = result["data"]["context"]
        for required in (
            "current_claims",
            "open_conflicts",
            "decisions",
            "open_questions",
            "work_items",
        ):
            self.assertIn(required, context)

    def test_agent_boundary_and_git_projection_workflow_are_explicit(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        normalized = " ".join(text.split())

        for required_statement in (
            "Proposal-only mutation boundary",
            "Never edit `.shared-mind/shared-mind.sqlite3` directly",
            "Markdown and JSON projections are non-authoritative",
            "Open fact conflicts are successful committed state",
            "Do not commit or push projection changes unless the user asks",
        ):
            self.assertIn(required_statement, normalized)
        self.assertIn("$ git status --short -- projections/", text)
        self.assertIn("$ git diff -- projections/", text)
        self.assertLess(
            text.index("$ shared-mind replay --verify"),
            text.index("$ shared-mind project --format markdown"),
        )


if __name__ == "__main__":
    unittest.main()
