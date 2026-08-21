from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind.adapters.session_hooks import main as hook_main
from shared_mind.product import ProductService
from shared_mind.session_bootstrap import write_project_binding
from shared_mind.workspace import Workspace


class SessionHookAdaptersTest(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "atlas"
        project.mkdir()
        (project / ".git").mkdir()
        return project

    def _run_hook(self, *argv: str, payload: dict[str, object]) -> dict[str, object]:
        output = io.StringIO()
        errors = io.StringIO()
        exit_code = hook_main(
            list(argv),
            stdin=io.StringIO(json.dumps(payload)),
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(0, exit_code, errors.getvalue())
        self.assertEqual("", errors.getvalue())
        return json.loads(output.getvalue())

    def _tree_bytes(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }

    def _event(self) -> dict[str, object]:
        return {
            "object_type": "TASK_TRACE_EVENT",
            "event_version": "task-trace-event@1",
            "event_id": "trace_event_dev105_neutral_0001",
            "sequence": 1,
            "event_type": "TOOL",
            "occurred_at": "2026-08-21T08:00:00Z",
            "summary": "Verify neutral project-bound capture",
            "details": {"tool_name": "Read"},
        }

    def test_claude_and_codex_session_start_emit_identical_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            workspace = Workspace.initialize(
                root / "atlas-memory", purpose="CROSS_CLIENT_CONTEXT_MARKER"
            )
            write_project_binding(project, workspace)
            payload = {
                "session_id": "session-dev-105",
                "cwd": str(project),
                "hook_event_name": "SessionStart",
                "source": "startup",
            }

            claude = self._run_hook("claude", "start", payload=payload)
            codex = self._run_hook("codex", "start", payload=payload)

        claude_output = claude["hookSpecificOutput"]
        codex_output = codex["hookSpecificOutput"]
        self.assertEqual("SessionStart", claude_output["hookEventName"])
        self.assertEqual("SessionStart", codex_output["hookEventName"])
        self.assertEqual(
            claude_output["additionalContext"],
            codex_output["additionalContext"],
        )
        self.assertIn("CROSS_CLIENT_CONTEXT_MARKER", claude_output["additionalContext"])

    def test_user_prompt_submit_refines_only_the_verified_project_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            workspace = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
            write_project_binding(project, workspace)

            output = self._run_hook(
                "codex",
                "prompt",
                payload={
                    "session_id": "session-dev-105",
                    "turn_id": "turn-dev-105",
                    "cwd": str(project),
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Review the payment adapter migration",
                },
            )

        hook_output = output["hookSpecificOutput"]
        self.assertEqual("UserPromptSubmit", hook_output["hookEventName"])
        self.assertIn(
            "Review the payment adapter migration",
            hook_output["additionalContext"],
        )
        self.assertIn(workspace.root.as_posix(), hook_output["additionalContext"])

    def test_missing_binding_emits_warning_without_additional_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary))

            output = self._run_hook(
                "claude",
                "start",
                payload={
                    "session_id": "session-dev-105",
                    "cwd": str(project),
                    "hook_event_name": "SessionStart",
                },
            )

        self.assertNotIn("hookSpecificOutput", output)
        self.assertIn("PROJECT_BINDING_NOT_FOUND", output["systemMessage"])

    def test_user_prompt_submit_changed_cwd_does_not_reuse_original_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_a = self._project(root)
            project_b = root / "beta"
            project_b.mkdir()
            (project_b / ".git").mkdir()
            workspace_a = Workspace.initialize(
                root / "atlas-memory", purpose="ORIGINAL_PROJECT_ONLY_MARKER"
            )
            Workspace.initialize(
                root / "beta-memory", purpose="UNBOUND_NEIGHBOR_MARKER"
            )
            write_project_binding(project_a, workspace_a)

            started = self._run_hook(
                "claude",
                "start",
                payload={"session_id": "session-dev-105", "cwd": str(project_a)},
            )
            prompted = self._run_hook(
                "claude",
                "prompt",
                payload={
                    "session_id": "session-dev-105",
                    "cwd": str(project_b),
                    "prompt": "Continue the original task",
                },
            )

        self.assertIn("ORIGINAL_PROJECT_ONLY_MARKER", str(started))
        self.assertNotIn("hookSpecificOutput", prompted)
        self.assertIn("PROJECT_BINDING_NOT_FOUND", prompted["systemMessage"])
        self.assertNotIn("ORIGINAL_PROJECT_ONLY_MARKER", str(prompted))

    def test_neutral_append_and_finalize_use_verified_cwd_binding_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_a = self._project(root)
            project_b = root / "beta"
            project_b.mkdir()
            (project_b / ".git").mkdir()
            workspace_a = Workspace.initialize(root / "atlas-memory", purpose="Atlas")
            workspace_b = Workspace.initialize(root / "beta-memory", purpose="Beta")
            write_project_binding(project_a, workspace_a)
            write_project_binding(project_b, workspace_b)
            neighbor_before = self._tree_bytes(workspace_b.root)
            session_id = "session:dev-105-neutral-capture"
            stale = workspace_b.root.as_posix()

            self._run_hook(
                "claude",
                "append",
                "--workspace",
                stale,
                payload={
                    "session_id": session_id,
                    "cwd": str(project_a),
                    "task_id": "DEV-105",
                    "event": self._event(),
                },
            )
            self._run_hook(
                "claude",
                "finalize",
                "--workspace",
                stale,
                payload={"session_id": session_id, "cwd": str(project_a)},
            )

            product_a = ProductService.open(workspace_a.root)
            try:
                self.assertEqual(1, len(product_a.store.list_batches()))
            finally:
                product_a.close()
            self.assertTrue(
                list((workspace_a.root / "observations" / "captured").glob("*.jsonl"))
            )
            self.assertFalse((workspace_b.root / "observations").exists())
            self.assertEqual(neighbor_before, self._tree_bytes(workspace_b.root))


if __name__ == "__main__":
    unittest.main()
