from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared_mind.session_bootstrap import bootstrap_session, write_project_binding
from shared_mind.workspace import Workspace


class ProjectSessionBootstrapTest(unittest.TestCase):
    def _project(self, root: Path, name: str) -> Path:
        project = root / name
        project.mkdir()
        (project / ".git").mkdir()
        (project / "src" / "pkg").mkdir(parents=True)
        return project

    def test_nested_cwd_uses_exact_project_binding_without_cross_project_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_a = self._project(root, "alpha")
            project_b = self._project(root, "beta")
            workspace_a = Workspace.initialize(
                root / "alpha-memory", purpose="ALPHA_ONLY_CONTEXT_MARKER"
            )
            workspace_b = Workspace.initialize(
                root / "beta-memory", purpose="BETA_ONLY_CONTEXT_MARKER"
            )
            write_project_binding(project_a, workspace_a)
            write_project_binding(project_b, workspace_b)

            result = bootstrap_session(cwd=project_a / "src" / "pkg")

        self.assertEqual("READY", result["status"], result)
        self.assertEqual(project_a.resolve().as_posix(), result["project_root"])
        self.assertEqual(workspace_a.root.as_posix(), result["workspace_root"])
        self.assertIn("ALPHA_ONLY_CONTEXT_MARKER", result["additional_context"])
        self.assertNotIn("BETA_ONLY_CONTEXT_MARKER", result["additional_context"])
        self.assertEqual(result["context"]["context_hash"], result["context_hash"])

    def test_missing_binding_does_not_load_conventional_sibling_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "atlas")
            Workspace.initialize(
                root / "atlas-memory", purpose="SHOULD_NOT_BE_AUTO_LOADED"
            )

            result = bootstrap_session(cwd=project / "src")

        self.assertEqual("PROJECT_BINDING_NOT_FOUND", result["status"], result)
        self.assertIsNone(result["additional_context"])
        self.assertNotIn("SHOULD_NOT_BE_AUTO_LOADED", str(result))

    def test_nested_git_repository_selects_its_own_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._project(root, "parent")
            child = parent / "vendor" / "child"
            child.mkdir(parents=True)
            (child / ".git").mkdir()
            workspace_parent = Workspace.initialize(
                root / "parent-memory", purpose="PARENT_ONLY_CONTEXT_MARKER"
            )
            workspace_child = Workspace.initialize(
                root / "child-memory", purpose="CHILD_ONLY_CONTEXT_MARKER"
            )
            write_project_binding(parent, workspace_parent)
            write_project_binding(child, workspace_child)

            result = bootstrap_session(cwd=child)

        self.assertEqual("READY", result["status"], result)
        self.assertEqual(child.resolve().as_posix(), result["project_root"])
        self.assertIn("CHILD_ONLY_CONTEXT_MARKER", result["additional_context"])
        self.assertNotIn("PARENT_ONLY_CONTEXT_MARKER", result["additional_context"])

    def test_invalid_binding_and_integrity_fail_closed_without_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "atlas")
            workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
            binding = write_project_binding(project, workspace)
            binding.write_text("{not-json\n", encoding="utf-8")

            result = bootstrap_session(cwd=project)

        self.assertEqual("PROJECT_BINDING_INVALID", result["status"], result)
        self.assertIsNone(result["additional_context"])
        self.assertIsNone(result["context_hash"])


if __name__ == "__main__":
    unittest.main()
