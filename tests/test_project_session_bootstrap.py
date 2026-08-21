from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_mind.canonical import canonical_json
from shared_mind.product import ProductService
from shared_mind.session_bootstrap import (
    binding_path_for,
    bootstrap_session,
    build_project_binding,
    write_project_binding,
)
from shared_mind.workspace import Workspace, WorkspaceError


class ProjectSessionBootstrapTest(unittest.TestCase):
    def _project(self, root: Path, name: str) -> Path:
        project = root / name
        project.mkdir()
        (project / ".git").mkdir()
        (project / "src" / "pkg").mkdir(parents=True)
        return project

    def _tree_bytes(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }

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

    def test_binding_schema_is_closed_and_rejects_extra_fields_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "atlas")
            workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
            binding = write_project_binding(project, workspace)
            document = json.loads(binding.read_text(encoding="utf-8"))
            document["client"] = "claude"
            binding.write_text(canonical_json(document) + "\n", encoding="utf-8")
            before = self._tree_bytes(workspace.root)

            result = bootstrap_session(cwd=project)

            self.assertEqual("PROJECT_BINDING_INVALID", result["status"], result)
            self.assertIsNone(result["additional_context"])
            self.assertIsNone(result["context_hash"])
            self.assertEqual(before, self._tree_bytes(workspace.root))

    def test_binding_root_and_workspace_hash_mismatches_fail_closed(self) -> None:
        cases = ("project_root", "workspace_config_hash")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = self._project(root, "atlas")
                workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
                binding = write_project_binding(project, workspace)
                document = json.loads(binding.read_text(encoding="utf-8"))
                if case == "project_root":
                    document[case] = (root / "other-project").resolve().as_posix()
                    expected = "PROJECT_BINDING_MISMATCH"
                else:
                    document[case] = "sha256:" + ("0" * 64)
                    expected = "WORKSPACE_BINDING_MISMATCH"
                binding.write_text(canonical_json(document) + "\n", encoding="utf-8")
                before = self._tree_bytes(workspace.root)

                result = bootstrap_session(cwd=project)

                self.assertEqual(expected, result["status"], result)
                self.assertIsNone(result["additional_context"])
                self.assertEqual(before, self._tree_bytes(workspace.root))

    def test_binding_file_and_control_directory_symlinks_fail_closed(self) -> None:
        for symlink_kind in ("file", "control"):
            with self.subTest(symlink=symlink_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = self._project(root, "atlas")
                workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
                binding = binding_path_for(project)
                binding.parent.mkdir(parents=True)
                content = (
                    canonical_json(build_project_binding(project, workspace)) + "\n"
                ).encode("utf-8")
                try:
                    if symlink_kind == "file":
                        target = root / "binding-target.json"
                        target.write_bytes(content)
                        binding.symlink_to(target)
                    else:
                        target = root / "binding-control"
                        target.mkdir()
                        (target / binding.name).write_bytes(content)
                        binding.parent.rmdir()
                        binding.parent.symlink_to(target, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink creation is unavailable: {exc}")

                result = bootstrap_session(cwd=project)

                self.assertEqual("PROJECT_BINDING_INVALID", result["status"], result)
                self.assertIsNone(result["additional_context"])

    def test_workspace_control_directory_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "atlas")
            workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
            write_project_binding(project, workspace)
            control = workspace.root / ".shared-mind"
            target = root / "workspace-control"
            control.rename(target)
            try:
                control.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            result = bootstrap_session(cwd=project)

            self.assertEqual("WORKSPACE_CONFIG_INVALID", result["status"], result)
            self.assertIsNone(result["additional_context"])

    def test_invalid_product_integrity_returns_no_context_and_mutates_no_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "atlas")
            workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
            write_project_binding(project, workspace)
            initialized = ProductService(workspace)
            initialized.close()
            before = self._tree_bytes(workspace.root)

            with patch.object(
                ProductService,
                "verify",
                return_value={"valid": False, "kernel": {"valid": False}},
            ):
                result = bootstrap_session(cwd=project)

            self.assertEqual("PRODUCT_INTEGRITY_INVALID", result["status"], result)
            self.assertIsNone(result["additional_context"])
            self.assertIsNone(result["context_hash"])
            self.assertEqual(before, self._tree_bytes(workspace.root))

    def test_write_project_binding_rejects_silent_workspace_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "atlas")
            first = Workspace.initialize(root / "first-memory", purpose="First")
            second = Workspace.initialize(root / "second-memory", purpose="Second")
            binding = write_project_binding(project, first)
            original = binding.read_bytes()

            unchanged = write_project_binding(project, first)
            with self.assertRaises(WorkspaceError) as caught:
                write_project_binding(project, second)

            self.assertEqual("PROJECT_BINDING_CONFLICT", caught.exception.code)
            self.assertEqual(binding, unchanged)
            self.assertEqual(original, binding.read_bytes())


if __name__ == "__main__":
    unittest.main()
