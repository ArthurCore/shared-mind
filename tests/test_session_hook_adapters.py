from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind.adapters.session_hooks import main as hook_main
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


if __name__ == "__main__":
    unittest.main()
