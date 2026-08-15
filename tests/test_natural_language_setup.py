from __future__ import annotations

import io
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_mind.cli import EXIT_OK, build_parser, main
from shared_mind.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / ".agents" / "skills" / "shared-mind-setup"


class NaturalLanguageSetupTest(unittest.TestCase):
    def _project(self, root: Path, name: str = "atlas") -> Path:
        project = root / name
        project.mkdir()
        (project / ".git").mkdir()
        (project / "README.md").write_text(
            "# Atlas\n\nA deterministic example project.\n", encoding="utf-8"
        )
        return project

    def _run_setup(
        self,
        project: Path,
        codex_home: Path,
        *arguments: str,
    ) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        previous = Path.cwd()
        os.chdir(project)
        try:
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                exit_code = main(["setup", *arguments], stdout=output)
        finally:
            os.chdir(previous)
        return exit_code, json.loads(output.getvalue())

    def test_setup_parser_and_skill_metadata_define_the_natural_language_surface(
        self,
    ) -> None:
        arguments = build_parser().parse_args(["setup"])

        self.assertEqual("setup", arguments.command)
        self.assertIsNone(arguments.project)
        self.assertIsNone(arguments.workspace_path)
        self.assertFalse(arguments.no_cold_start)
        self.assertFalse(arguments.no_install_skill)
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: shared-mind-setup", skill_text)
        self.assertIn("Shared Mind 초기설정해", skill_text)
        self.assertNotIn("shared mine", skill_text.lower())
        self.assertIn("shared-mind setup", skill_text)
        interface = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn('display_name: "Shared Mind Setup"', interface)

    def test_setup_creates_one_sibling_workspace_installs_skill_and_returns_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            codex_home = root / "codex-home"

            exit_code, result = self._run_setup(project, codex_home)

            self.assertEqual(EXIT_OK, exit_code, result)
            self.assertEqual("SETUP_READY", result["code"])
            data = result["data"]
            self.assertEqual(project.resolve().as_posix(), data["project"])
            self.assertEqual((root / "atlas-memory").resolve().as_posix(), data["workspace"])
            self.assertTrue(data["workspace_created"])
            self.assertTrue(data["cold_start"]["performed"])
            self.assertEqual("INSTALLED", data["codex_skill"]["status"])
            self.assertTrue(data["integrity"]["valid"])
            self.assertEqual(
                "Continue the highest-priority unblocked project work.",
                data["context"]["request"]["task"],
            )
            self.assertTrue(
                (root / "atlas-memory" / ".shared-mind" / "workspace.json").is_file()
            )
            installed = codex_home / "skills" / "shared-mind-setup"
            self.assertEqual(
                (SKILL_ROOT / "SKILL.md").read_bytes(),
                (installed / "SKILL.md").read_bytes(),
            )
            self.assertTrue((installed / "agents" / "openai.yaml").is_file())

    def test_setup_is_idempotent_for_workspace_cold_start_and_skill_install(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            codex_home = root / "codex-home"

            first_exit, first = self._run_setup(project, codex_home)
            second_exit, second = self._run_setup(project, codex_home)

            self.assertEqual(EXIT_OK, first_exit, first)
            self.assertEqual(EXIT_OK, second_exit, second)
            self.assertTrue(first["data"]["workspace_created"])
            self.assertFalse(second["data"]["workspace_created"])
            self.assertTrue(first["data"]["cold_start"]["performed"])
            self.assertFalse(second["data"]["cold_start"]["performed"])
            self.assertEqual("UNCHANGED", second["data"]["codex_skill"]["status"])
            self.assertEqual(
                first["data"]["context"]["ledger_sequence"],
                second["data"]["context"]["ledger_sequence"],
            )
            self.assertEqual(
                first["data"]["context"]["kernel_state_root"],
                second["data"]["context"]["kernel_state_root"],
            )

    def test_setup_retries_an_incomplete_cold_start_without_duplicate_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            codex_home = root / "codex-home"

            initial_exit, initial = self._run_setup(
                project, codex_home, "--no-cold-start"
            )
            completed_exit, completed = self._run_setup(project, codex_home)
            repeated_exit, repeated = self._run_setup(project, codex_home)

            self.assertEqual(EXIT_OK, initial_exit, initial)
            self.assertFalse(initial["data"]["cold_start"]["performed"])
            self.assertEqual(EXIT_OK, completed_exit, completed)
            self.assertTrue(completed["data"]["cold_start"]["performed"])
            self.assertEqual(EXIT_OK, repeated_exit, repeated)
            self.assertFalse(repeated["data"]["cold_start"]["performed"])
            self.assertEqual(
                completed["data"]["context"]["ledger_sequence"],
                repeated["data"]["context"]["ledger_sequence"],
            )

    def test_setup_repairs_only_stale_disposable_views_before_resuming(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            codex_home = root / "codex-home"
            initial_exit, initial = self._run_setup(project, codex_home)
            self.assertEqual(EXIT_OK, initial_exit, initial)
            workspace = Workspace.open(root / "atlas-memory")
            late_source = workspace.source_root / "late.md"
            late_source.write_text("Late evidence without directives.\n", encoding="utf-8")
            _, receipt = workspace.add_source(late_source)
            self.assertEqual("COMMITTED", receipt.outcome)

            resumed_exit, resumed = self._run_setup(project, codex_home)

            self.assertEqual(EXIT_OK, resumed_exit, resumed)
            self.assertEqual("SETUP_READY", resumed["code"])
            self.assertTrue(resumed["data"]["consolidation"]["performed"])
            self.assertTrue(resumed["data"]["consolidation"]["changed_artifact_ids"])
            self.assertTrue(resumed["data"]["integrity"]["valid"])
            self.assertEqual(receipt.ledger_seq, resumed["data"]["context"]["ledger_sequence"])

    def test_setup_expands_only_to_the_minimum_budget_required_by_continuity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            (project / "README.md").write_text(
                "# Atlas\n\n"
                + "\n".join(
                    f"WORK: P1 | Preserve mandatory setup task number {index:02d}."
                    for index in range(40)
                )
                + "\n",
                encoding="utf-8",
            )

            exit_code, result = self._run_setup(project, root / "codex-home")

            self.assertEqual(EXIT_OK, exit_code, result)
            budget = result["data"]["context"]["budget"]["budget_bytes"]
            self.assertGreater(budget, 24 * 1024)
            self.assertLess(budget, 128 * 1024)
            self.assertEqual(
                40, len(result["data"]["context"]["core_context"]["work_items"])
            )

    def test_setup_fails_closed_on_an_unmanaged_conflicting_global_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            codex_home = root / "codex-home"
            destination = codex_home / "skills" / "shared-mind-setup"
            destination.mkdir(parents=True)
            custom = b"---\nname: custom\ndescription: custom\n---\n"
            (destination / "SKILL.md").write_bytes(custom)

            exit_code, result = self._run_setup(project, codex_home)

            self.assertNotEqual(EXIT_OK, exit_code)
            self.assertEqual("CODEX_SKILL_CONFLICT", result["code"])
            self.assertEqual(custom, (destination / "SKILL.md").read_bytes())
            self.assertFalse((root / "atlas-memory").exists())

    def test_implicit_setup_requires_a_git_project_but_explicit_project_is_allowed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain = root / "plain"
            plain.mkdir()
            (plain / "README.md").write_text("# Plain\n", encoding="utf-8")
            codex_home = root / "codex-home"

            failed_exit, failed = self._run_setup(
                plain, codex_home, "--no-cold-start"
            )
            explicit_exit, explicit = self._run_setup(
                plain,
                codex_home,
                "--project",
                str(plain),
                "--no-cold-start",
            )

            self.assertNotEqual(EXIT_OK, failed_exit)
            self.assertEqual("PROJECT_ROOT_NOT_FOUND", failed["code"])
            self.assertEqual(EXIT_OK, explicit_exit, explicit)
            self.assertEqual("SETUP_READY", explicit["code"])

    def test_package_and_docs_ship_the_global_natural_language_skill(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        data_files = metadata["tool"]["setuptools"]["data-files"]
        packaged = data_files["share/shared-mind/skills/shared-mind-setup"]
        packaged_agents = data_files[
            "share/shared-mind/skills/shared-mind-setup/agents"
        ]

        self.assertIn(".agents/skills/shared-mind-setup/SKILL.md", packaged)
        self.assertIn(
            ".agents/skills/shared-mind-setup/agents/openai.yaml", packaged_agents
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        bootstrap = (ROOT / "docs" / "agent-bootstrap.md").read_text(
            encoding="utf-8"
        )
        for document in (readme, bootstrap):
            self.assertIn("Shared Mind 초기설정해", document)
            self.assertIn("$ shared-mind setup", document)


if __name__ == "__main__":
    unittest.main()
