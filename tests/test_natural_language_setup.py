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
from shared_mind.session_bootstrap import (
    binding_path_for,
    bootstrap_session,
    write_project_binding,
)
from shared_mind.setup import setup_project
from shared_mind.workspace import Workspace, WorkspaceError


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

    def _direct_setup(
        self,
        project: Path,
        *,
        workspace: Path | None = None,
        install_hooks: bool = False,
    ) -> dict[str, object]:
        return setup_project(
            start=project,
            workspace_path=workspace,
            cold_start=False,
            install_codex_skill=False,
            install_claude_hooks=install_hooks,
        )

    def test_setup_parser_and_skill_metadata_define_the_natural_language_surface(
        self,
    ) -> None:
        arguments = build_parser().parse_args(["setup"])

        self.assertEqual("setup", arguments.command)
        self.assertIsNone(arguments.project)
        self.assertIsNone(arguments.workspace_path)
        self.assertFalse(arguments.no_cold_start)
        self.assertFalse(arguments.no_install_skill)
        self.assertFalse(arguments.install_hooks)
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: shared-mind-setup", skill_text)
        self.assertIn("Shared Mind 초기설정해", skill_text)
        self.assertNotIn("shared mine", skill_text.lower())
        self.assertIn("shared-mind setup", skill_text)
        self.assertIn("shared-mind-product observe finalize", skill_text)
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

    def test_nested_project_implicit_setup_never_reuses_ancestor_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._project(root, "parent")
            child = parent / "vendor" / "child"
            child.mkdir(parents=True)
            (child / ".git").mkdir()
            (child / "README.md").write_text("# Child\n", encoding="utf-8")
            ancestor = Workspace.initialize(
                root / "parent-memory", purpose="ANCESTOR_MEMORY_MUST_NOT_BE_REUSED"
            )

            result = self._direct_setup(child)

            expected = child.parent / "child-memory"
            self.assertEqual(expected.resolve().as_posix(), result["workspace"])
            self.assertTrue(result["workspace_created"])
            self.assertEqual("ANCESTOR_MEMORY_MUST_NOT_BE_REUSED", ancestor.purpose)

    def test_nested_project_implicit_setup_reuses_only_its_verified_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._project(root, "parent")
            child = parent / "vendor" / "child"
            child.mkdir(parents=True)
            (child / ".git").mkdir()
            (child / "README.md").write_text("# Child\n", encoding="utf-8")
            Workspace.initialize(root / "parent-memory", purpose="Ancestor")
            bound = Workspace.initialize(root / "child-custom-memory", purpose="Child")
            write_project_binding(child, bound)

            result = self._direct_setup(child)

            self.assertEqual(bound.root.as_posix(), result["workspace"])
            self.assertFalse(result["workspace_created"])

    def test_existing_binding_conflicts_fail_without_explicit_workspace(self) -> None:
        for case in ("malformed", "project-root", "workspace-config"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = self._project(root)
                workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
                binding = write_project_binding(project, workspace)
                if case == "malformed":
                    binding.write_bytes(b"{not-json\n")
                    expected = "PROJECT_BINDING_INVALID"
                else:
                    document = json.loads(binding.read_text(encoding="utf-8"))
                    if case == "project-root":
                        document["project_root"] = (root / "other").as_posix()
                        expected = "PROJECT_BINDING_MISMATCH"
                    else:
                        document["workspace_config_hash"] = "sha256:" + ("0" * 64)
                        expected = "WORKSPACE_BINDING_MISMATCH"
                    binding.write_text(
                        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                before = binding.read_bytes()

                with self.assertRaises(WorkspaceError) as caught:
                    self._direct_setup(project, install_hooks=True)

                self.assertEqual(expected, caught.exception.code)
                self.assertEqual(before, binding.read_bytes())

    def test_explicit_workspace_is_the_only_rebind_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            old = Workspace.initialize(root / "old-memory", purpose="Old")
            new = Workspace.initialize(root / "new-memory", purpose="New")
            write_project_binding(project, old)

            result = self._direct_setup(
                project, workspace=new.root, install_hooks=True
            )
            resumed = bootstrap_session(cwd=project)

            self.assertEqual(new.root.as_posix(), result["workspace"])
            self.assertEqual("READY", resumed["status"], resumed)
            self.assertEqual(new.root.as_posix(), resumed["workspace_root"])
            self.assertIn("New", resumed["additional_context"])

    def test_hook_install_publishes_binding_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
            targets = [
                project / ".claude" / "settings.json",
                project / ".codex" / "hooks.json",
                binding_path_for(project),
            ]
            replaced: list[Path] = []
            original_replace = os.replace

            def record_replace(source: object, destination: object) -> None:
                target = Path(destination).resolve()
                if target in {path.resolve() for path in targets}:
                    replaced.append(target)
                original_replace(source, destination)

            with patch("shared_mind.setup.os.replace", side_effect=record_replace):
                self._direct_setup(
                    project, workspace=workspace.root, install_hooks=True
                )

            self.assertEqual([path.resolve() for path in targets], replaced)

    def test_hook_install_rollback_restores_every_existing_file(self) -> None:
        for failure_index in (1, 2, 3):
            with self.subTest(failure_after_replace=failure_index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = self._project(root)
                old = Workspace.initialize(root / "old-memory", purpose="Old")
                new = Workspace.initialize(root / "new-memory", purpose="New")
                binding = write_project_binding(project, old)
                settings = project / ".claude" / "settings.json"
                settings.parent.mkdir()
                settings.write_bytes(b'{"permissions":{"allow":["Read"]}}\n')
                codex = project / ".codex" / "hooks.json"
                codex.parent.mkdir()
                codex.write_bytes(b'{"Notification":[]}\n')
                targets = [settings, codex, binding]
                originals = {path: path.read_bytes() for path in targets}
                original_replace = os.replace
                target_count = 0
                injected = False

                def fail_replace(source: object, destination: object) -> None:
                    nonlocal target_count, injected
                    target = Path(destination).resolve()
                    if target in {path.resolve() for path in targets} and not injected:
                        target_count += 1
                        if target_count == failure_index:
                            injected = True
                            raise OSError(f"injected replace failure {failure_index}")
                    original_replace(source, destination)

                with patch("shared_mind.setup.os.replace", side_effect=fail_replace):
                    with self.assertRaises((OSError, WorkspaceError)):
                        self._direct_setup(
                            project, workspace=new.root, install_hooks=True
                        )

                self.assertTrue(injected)
                for path, original in originals.items():
                    self.assertEqual(original, path.read_bytes(), path)

    def test_hook_install_rollback_removes_every_new_destination(self) -> None:
        for failure_index in (1, 2, 3):
            with self.subTest(failure_after_replace=failure_index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = self._project(root)
                workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
                targets = [
                    project / ".claude" / "settings.json",
                    project / ".codex" / "hooks.json",
                    binding_path_for(project),
                ]
                original_replace = os.replace
                target_count = 0
                injected = False

                def fail_replace(source: object, destination: object) -> None:
                    nonlocal target_count, injected
                    target = Path(destination).resolve()
                    if target in {path.resolve() for path in targets} and not injected:
                        target_count += 1
                        if target_count == failure_index:
                            injected = True
                            raise OSError(f"injected replace failure {failure_index}")
                    original_replace(source, destination)

                with patch("shared_mind.setup.os.replace", side_effect=fail_replace):
                    with self.assertRaises((OSError, WorkspaceError)):
                        self._direct_setup(
                            project, workspace=workspace.root, install_hooks=True
                        )

                self.assertTrue(injected)
                for path in targets:
                    self.assertFalse(path.exists(), path)

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

    def test_setup_does_not_touch_claude_settings_without_install_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            settings = project / ".claude" / "settings.json"
            settings.parent.mkdir()
            original = (
                b'{"hooks":{"PreToolUse":[{"hooks":[{"command":"keep-claude",'
                b'"type":"command"}],"matcher":"Read"}]},'
                b'"permissions":{"allow":["Read"]}}\n'
            )
            settings.write_bytes(original)
            codex_path = project / ".codex" / "hooks.json"
            codex_path.parent.mkdir()
            codex_original = (
                b'{"Notification":[{"hooks":[{"command":"keep-codex",'
                b'"type":"command"}]}]}\n'
            )
            codex_path.write_bytes(codex_original)
            codex_home = root / "codex-home"

            default_exit, default = self._run_setup(
                project, codex_home, "--no-cold-start"
            )

            self.assertEqual(EXIT_OK, default_exit, default)
            self.assertEqual(original, settings.read_bytes())
            self.assertEqual(codex_original, codex_path.read_bytes())
            self.assertFalse((project / ".shared-mind" / "project-binding.json").exists())
            self.assertEqual("SKIPPED", default["data"]["claude_hooks"]["status"])
            self.assertEqual("SKIPPED", default["data"]["codex_hooks"]["status"])
            self.assertEqual("SKIPPED", default["data"]["project_binding"]["status"])

            installed_exit, installed = self._run_setup(
                project,
                codex_home,
                "--no-cold-start",
                "--install-hooks",
            )
            repeated_exit, repeated = self._run_setup(
                project,
                codex_home,
                "--no-cold-start",
                "--install-hooks",
            )

            self.assertEqual(EXIT_OK, installed_exit, installed)
            self.assertEqual(EXIT_OK, repeated_exit, repeated)
            configured = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(["Read"], configured["permissions"]["allow"])
            self.assertEqual(
                "keep-claude",
                configured["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            )
            self.assertIn("SessionStart", configured["hooks"])
            self.assertIn("UserPromptSubmit", configured["hooks"])
            self.assertIn("PostToolUse", configured["hooks"])
            self.assertIn("SessionEnd", configured["hooks"])
            self.assertIn("Stop", configured["hooks"])
            binding = json.loads(
                (project / ".shared-mind" / "project-binding.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(project.resolve().as_posix(), binding["project_root"])
            self.assertEqual(
                (root / "atlas-memory").resolve().as_posix(),
                binding["workspace_root"],
            )
            codex_hooks = json.loads(
                (project / ".codex" / "hooks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "keep-codex",
                codex_hooks["Notification"][0]["hooks"][0]["command"],
            )
            self.assertIn("SessionStart", codex_hooks)
            self.assertIn("UserPromptSubmit", codex_hooks)
            self.assertIn("PostToolUse", codex_hooks)
            self.assertIn("SessionEnd", codex_hooks)
            self.assertEqual(
                12000,
                codex_hooks["SessionStart"][0]["hooks"][0]["additionalContextLimit"],
            )
            for event, action in {
                "SessionStart": "start",
                "UserPromptSubmit": "prompt",
                "PostToolUse": "append",
                "SessionEnd": "finalize",
                "Stop": "finalize",
            }.items():
                command = configured["hooks"][event][-1]["hooks"][0]["command"]
                self.assertEqual(f"shared-mind-session-hook claude {action}", command)
            for event, action in {
                "SessionStart": "start",
                "UserPromptSubmit": "prompt",
                "PostToolUse": "append",
                "SessionEnd": "finalize",
            }.items():
                hook = codex_hooks[event][-1]["hooks"][0]
                self.assertEqual(
                    f"shared-mind-session-hook codex {action}", hook["command"]
                )
                self.assertNotIn(project.as_posix(), hook["command"])
                self.assertNotIn((root / "atlas-memory").as_posix(), hook["command"])
                if event in {"SessionStart", "UserPromptSubmit"}:
                    self.assertGreaterEqual(hook["additionalContextLimit"], 6144)
                    self.assertLessEqual(hook["additionalContextLimit"], 16384)
            self.assertIn(
                installed["data"]["claude_hooks"]["status"],
                {"INSTALLED", "UPDATED"},
            )
            self.assertIn(
                installed["data"]["codex_hooks"]["status"],
                {"INSTALLED", "UPDATED"},
            )
            self.assertEqual("INSTALLED", installed["data"]["project_binding"]["status"])
            self.assertEqual("UNCHANGED", repeated["data"]["claude_hooks"]["status"])
            self.assertEqual("UNCHANGED", repeated["data"]["codex_hooks"]["status"])
            self.assertEqual("UNCHANGED", repeated["data"]["project_binding"]["status"])

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
