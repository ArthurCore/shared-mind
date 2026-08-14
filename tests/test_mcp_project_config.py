from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 test environment only
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".codex" / "config.toml"
GUIDE_PATH = ROOT / "docs" / "mcp.md"

TOOL_NAMES = (
    "context",
    "query",
    "proposal_validate",
    "proposal_commit",
    "source_add",
    "conflict_list",
    "ledger_verify",
)
RESOURCE_URIS = (
    "shared-mind://workspace/info",
    "shared-mind://workspace/context",
    "shared-mind://projection/project.json",
    "shared-mind://projection/project.md",
    "shared-mind://contract/schema",
    "shared-mind://contract/predicate-registry",
)
ROLE_FILES = {
    "explorer": "explorer.toml",
    "reviewer": "reviewer.toml",
    "docs_researcher": "docs-researcher.toml",
}


class McpProjectConfigurationContractTest(unittest.TestCase):
    def test_project_config_enables_multi_agent_and_safe_local_stdio(self) -> None:
        config = self.load_toml(CONFIG_PATH)

        self.assertIs(config["features"]["multi_agent"], True)
        server = config["mcp_servers"]["shared_mind"]
        self.assertEqual("shared-mind-mcp", server["command"])
        self.assertEqual(["--workspace", "../shared-mind-memory"], server["args"])
        self.assertEqual(".", server["cwd"])
        self.assertIs(server["required"], False)
        self.assertNotIn("env", server)
        self.assertNotIn("env_vars", server)
        self.assertFalse(self.contains_secret_key(server), server)
        self.assertFalse(self.contains_personal_absolute_path(server), server)

    def test_three_codex_roles_use_project_local_read_only_layers(self) -> None:
        config = self.load_toml(CONFIG_PATH)
        agents = config.get("agents", {})

        for role, filename in ROLE_FILES.items():
            with self.subTest(role=role):
                # Standalone project roles are auto-discovered below
                # .codex/agents. Duplicating them as config_file overlays is
                # error-prone because those paths resolve relative to
                # .codex/config.toml, not the repository root.
                self.assertNotIn(role, agents)
                role_path = CONFIG_PATH.parent / "agents" / filename
                self.assertTrue(role_path.is_file(), role_path)
                layer = self.load_toml(role_path)
                self.assertEqual(role, layer["name"])
                self.assertTrue(str(layer["description"]).strip())
                self.assertTrue(str(layer["developer_instructions"]).strip())
                self.assertEqual("read-only", layer["sandbox_mode"])
                instructions = str(layer.get("developer_instructions", "")).lower()
                self.assertIn("read-only", instructions)
                self.assertTrue(
                    "do not edit" in instructions
                    or "no file changes" in instructions
                    or "must not modify" in instructions,
                    instructions,
                )
                self.assertNotIn("mcp_servers", layer)
                self.assertFalse(self.contains_secret_key(layer), layer)

    def test_guide_distinguishes_base_and_optional_mcp_installs(self) -> None:
        guide = self.read_guide()
        normalized = " ".join(guide.split())
        project = self.load_toml(ROOT / "pyproject.toml")["project"]

        self.assertEqual(">=3.11", project["requires-python"])
        self.assertEqual(["mcp>=2,<3"], project["optional-dependencies"]["mcp"])
        self.assertRegex(guide, r"Python\s+3\.11\+?")
        self.assertIn("python3 -m pip install .", guide)
        self.assertRegex(
            guide,
            r"python3 -m pip install\s+['\"]?\.\[mcp\]['\"]?",
        )
        self.assertIn("mcp>=2,<3", guide)
        self.assertRegex(
            normalized.lower(),
            r"base install.{0,160}(does not|doesn't|without).{0,80}mcp sdk",
        )

    def test_guide_lists_the_exact_tool_and_resource_allowlists(self) -> None:
        guide = self.read_guide()

        tools_section = self.markdown_section(guide, "Tools")
        resources_section = self.markdown_section(guide, "Resources")
        self.assertEqual(
            TOOL_NAMES,
            tuple(re.findall(r"^\|\s*`([^`]+)`\s*\|", tools_section, re.MULTILINE)),
        )
        self.assertEqual(
            RESOURCE_URIS,
            tuple(
                re.findall(
                    r"^\|\s*`(shared-mind://[^`]+)`\s*\|",
                    resources_section,
                    re.MULTILINE,
                )
            ),
        )
        normalized = " ".join(guide.lower().split())
        self.assertIn("source_add", normalized)
        self.assertIn("relative", normalized)
        self.assertIn("source root", normalized)
        self.assertRegex(
            normalized,
            r"(does not|never|no) (expose|provide)[^.]{0,100}(arbitrary|database|sql|file)",
        )

    def test_guide_documents_trust_approval_and_stdout_boundaries(self) -> None:
        guide = self.read_guide()
        normalized = " ".join(guide.lower().split())

        self.assertIn("trust", normalized)
        self.assertIn("approval", normalized)
        self.assertIn("proposal_commit", normalized)
        self.assertIn("source_add", normalized)
        self.assertRegex(
            normalized,
            r"(tool annotations|hints).{0,100}(not|do not|does not).{0,80}(enforce|authorization|approval)",
        )
        self.assertRegex(
            normalized,
            r"stdout.{0,100}(json-rpc|protocol).{0,100}(stderr|diagnostic)",
        )
        self.assertIn("shared-mind-mcp", normalized)
        self.assertIn("troubleshooting", normalized)
        self.assertIn("version", normalized)

    def test_guide_has_generic_claude_stdio_json_without_external_writes(self) -> None:
        guide = self.read_guide()
        claude_section = self.markdown_section(guide, "Claude Desktop and Claude Code")

        self.assertIn("Claude Desktop", claude_section)
        self.assertIn("Claude Code", claude_section)
        self.assertRegex(claude_section, r'"command"\s*:\s*"shared-mind-mcp"')
        self.assertRegex(
            claude_section,
            r'"args"\s*:\s*\[\s*"--workspace"\s*,\s*"\."\s*\]',
        )
        self.assertRegex(
            claude_section.lower(),
            r"(example|generic).{0,120}(does not|do not|won't|will not).{0,80}(write|modify|edit)",
        )
        forbidden = (
            "~/.claude",
            "Library/Application Support/Claude",
            "/Users/",
            "/home/",
            "api_key",
            "access_token",
            "client_secret",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value.lower(), claude_section.lower())
        self.assertNotRegex(claude_section, r"(?m)^\s*(cp|mv|tee)\s")
        self.assertNotRegex(claude_section, r"(?m)^.*>\s*[/~]")

    @staticmethod
    def load_toml(path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            return tomllib.load(handle)

    @staticmethod
    def read_guide() -> str:
        return GUIDE_PATH.read_text(encoding="utf-8")

    @staticmethod
    def markdown_section(document: str, title: str) -> str:
        match = re.search(
            rf"(?ms)^##\s+{re.escape(title)}\s*$\n(.*?)(?=^##\s+|\Z)",
            document,
        )
        if match is None:
            raise AssertionError(f"Missing Markdown section: {title}")
        return match.group(1)

    @classmethod
    def contains_secret_key(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if any(
                    marker in normalized
                    for marker in ("secret", "password", "api_key", "access_token")
                ):
                    return True
                if cls.contains_secret_key(item):
                    return True
        elif isinstance(value, list):
            return any(cls.contains_secret_key(item) for item in value)
        return False

    @classmethod
    def contains_personal_absolute_path(cls, value: Any) -> bool:
        if isinstance(value, dict):
            return any(cls.contains_personal_absolute_path(item) for item in value.values())
        if isinstance(value, list):
            return any(cls.contains_personal_absolute_path(item) for item in value)
        if not isinstance(value, str):
            return False
        normalized = value.replace("\\", "/")
        return bool(
            re.search(r"(^|\s)/(Users|home)/[^/\s]+/", normalized)
            or re.search(r"(?i)[A-Z]:/Users/[^/\s]+/", normalized)
        )


if __name__ == "__main__":
    unittest.main()
