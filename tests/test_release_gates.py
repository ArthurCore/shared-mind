from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GIT_ATTRIBUTES = ROOT / ".gitattributes"


def _distribution_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    if match is None:
        raise AssertionError(f"cannot parse dependency name from {requirement!r}")
    return match.group(1).lower().replace("_", "-")


def _yaml_blocks(document: str, key: str) -> list[str]:
    """Extract indented YAML blocks without adding a runtime YAML dependency."""

    lines = document.splitlines()
    blocks: list[str] = []
    pattern = re.compile(rf"^(?P<indent>\s*){re.escape(key)}:\s*(?:#.*)?$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match is None:
            continue
        indentation = len(match.group("indent"))
        body = [line]
        for candidate in lines[index + 1 :]:
            if not candidate.strip():
                body.append(candidate)
                continue
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate_indent <= indentation:
                break
            body.append(candidate)
        blocks.append("\n".join(body))
    return blocks


class ReleaseGatePresenceTest(unittest.TestCase):
    def test_dev_025_026_ci_workflow_exists(self) -> None:
        self.assertTrue(
            CI_WORKFLOW.is_file(),
            "DEV-025/026 requires .github/workflows/ci.yml",
        )

    def test_cross_platform_fixtures_pin_lf_checkout_bytes(self) -> None:
        self.assertTrue(
            GIT_ATTRIBUTES.is_file(),
            "content-addressed fixtures need a repository line-ending policy",
        )
        rules = {
            line.strip()
            for line in GIT_ATTRIBUTES.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("* text=auto eol=lf", rules)


@unittest.skipUnless(
    CI_WORKFLOW.is_file(),
    "release workflow has not been implemented yet (expected RED)",
)
class ReleaseGateStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with PYPROJECT.open("rb") as handle:
            cls.pyproject = tomllib.load(handle)
        cls.workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        cls.workflow_lower = cls.workflow.lower()

    def test_read_contract_is_installed_with_the_base_package(self) -> None:
        packaged_contracts = self.pyproject["tool"]["setuptools"]["data-files"][
            "share/shared-mind/contracts"
        ]
        self.assertIn(
            "contracts/shared-mind-read.schema.v1.json",
            packaged_contracts,
        )

    def test_mcp_server_is_an_optional_bounded_extra_and_console_entrypoint(
        self,
    ) -> None:
        optional = self.pyproject["project"]["optional-dependencies"]
        self.assertIn("mcp", optional)
        self.assertIn(
            "mcp>=2,<3",
            {requirement.replace(" ", "") for requirement in optional["mcp"]},
        )
        self.assertEqual(
            "shared_mind.mcp_server:main",
            self.pyproject["project"]["scripts"]["shared-mind-mcp"],
        )
        base_names = {
            _distribution_name(requirement)
            for requirement in self.pyproject["project"].get("dependencies", [])
        }
        self.assertNotIn("mcp", base_names)

    def test_product_entrypoints_and_contracts_are_packaged(self) -> None:
        scripts = self.pyproject["project"]["scripts"]
        self.assertEqual(
            "shared_mind.product_cli:main", scripts["shared-mind-product"]
        )
        self.assertEqual(
            "shared_mind.product_mcp_server:main",
            scripts["shared-mind-product-mcp"],
        )
        self.assertEqual(
            "shared_mind.web_control:main", scripts["shared-mind-web"]
        )
        packaged_contracts = self.pyproject["tool"]["setuptools"]["data-files"][
            "share/shared-mind/contracts"
        ]
        self.assertIn(
            "contracts/shared-mind-product.schema.v1.json", packaged_contracts
        )
        self.assertIn(
            "contracts/product-conformance-fixtures.v1.json", packaged_contracts
        )

    def test_dev_or_quality_extras_supply_every_local_release_gate(self) -> None:
        optional = self.pyproject["project"]["optional-dependencies"]
        gate_extra_names = {"dev", "quality"}.intersection(optional)
        self.assertTrue(
            gate_extra_names,
            "declare a dev and/or quality optional dependency group",
        )
        requirements = [
            requirement
            for extra in gate_extra_names
            for requirement in optional[extra]
        ]
        names = {_distribution_name(requirement) for requirement in requirements}
        self.assertTrue(
            {
                "coverage",
                "ruff",
                "pip-audit",
                "bandit",
                "build",
                "twine",
            }.issubset(names),
            names,
        )
        self.assertTrue({"mypy", "pyright"}.intersection(names), names)

    def test_ci_uses_minimum_read_permissions_and_safe_pull_request_trigger(
        self,
    ) -> None:
        top_level_permissions = [
            block
            for block in _yaml_blocks(self.workflow, "permissions")
            if block.splitlines()[0] == "permissions:"
        ]
        self.assertEqual(1, len(top_level_permissions))
        permission_pairs = dict(
            re.findall(
                r"(?m)^  ([a-z-]+):\s*([a-z-]+)\s*$",
                top_level_permissions[0],
            )
        )
        self.assertEqual({"contents": "read"}, permission_pairs)
        self.assertNotRegex(self.workflow, r"(?im):\s*write\s*$")
        self.assertNotIn("pull_request_target", self.workflow)
        self.assertRegex(self.workflow, r"(?m)^\s{2}pull_request:\s*$")
        self.assertRegex(self.workflow, r"(?m)^\s{2}push:\s*$")
        self.assertRegex(
            self.workflow,
            r"(?m)^\s+persist-credentials:\s*false\s*$",
        )

    def test_every_remote_action_is_pinned_to_a_full_commit_sha(self) -> None:
        references = re.findall(
            r"(?m)^\s*-?\s*uses:\s*[\"']?([^\"'\s#]+)",
            self.workflow,
        )
        self.assertGreaterEqual(len(references), 2, references)
        self.assertTrue(
            any(reference.startswith("actions/checkout@") for reference in references),
            references,
        )
        self.assertTrue(
            any(
                reference.startswith("actions/setup-python@")
                for reference in references
            ),
            references,
        )
        for reference in references:
            if reference.startswith("./"):
                continue
            with self.subTest(action=reference):
                self.assertRegex(reference, r"^[^@\s]+@[0-9a-fA-F]{40}$")

    def test_supported_python_versions_are_all_exercised_by_a_matrix(self) -> None:
        matrix_text = "\n".join(_yaml_blocks(self.workflow, "matrix"))
        self.assertIn("python-version:", matrix_text)
        for version in ("3.11", "3.12", "3.13"):
            with self.subTest(python=version):
                self.assertRegex(
                    matrix_text,
                    rf"[\"']{re.escape(version)}[\"']",
                )

    def test_determinism_subset_runs_on_linux_macos_and_windows(self) -> None:
        matrix_text = "\n".join(_yaml_blocks(self.workflow, "matrix")).lower()
        for operating_system in ("ubuntu-", "macos-", "windows-"):
            with self.subTest(os=operating_system):
                self.assertIn(operating_system, matrix_text)
        self.assertIn("determin", self.workflow_lower)
        self.assertIn("test_projection.py", self.workflow_lower)
        self.assertIn("test_structured_query.py", self.workflow_lower)
        self.assertIn("test_memory_views_product.py", self.workflow_lower)
        self.assertIn("test_product_retrieval.py", self.workflow_lower)
        self.assertIn("test_observe.py", self.workflow_lower)
        self.assertIn("test_project_session_bootstrap.py", self.workflow_lower)
        self.assertIn("test_session_hook_adapters.py", self.workflow_lower)

    def test_contract_validation_and_full_suite_enforce_80_percent_coverage(
        self,
    ) -> None:
        self.assertIn("contracts/validate_contract.py", self.workflow)
        self.assertIn("contracts/validate_product_contract.py", self.workflow)
        self.assertIn("tools/run_parallel_coverage.py", self.workflow)
        self.assertIn("--workers 2", self.workflow)
        self.assertIn("--fail-under 80", self.workflow)

    def test_compile_lint_type_and_security_commands_are_blocking_gates(self) -> None:
        self.assertIn("compileall", self.workflow_lower)
        self.assertRegex(self.workflow_lower, r"\bruff\s+check\b")
        self.assertRegex(self.workflow_lower, r"\b(?:mypy|pyright)\b")
        self.assertRegex(self.workflow_lower, r"\bpip-audit\b")
        self.assertRegex(self.workflow_lower, r"\bbandit\b[^\n]*(?:-r|src)")

    def test_dependency_audit_skips_only_the_local_editable_distribution(self) -> None:
        self.assertRegex(
            self.workflow_lower,
            r"pip\s+freeze\s+--exclude-editable[^\n]*audit-requirements",
        )
        self.assertRegex(
            self.workflow_lower,
            r"pip-audit\s+--strict\s+-r\s+[^\n]*audit-requirements",
        )
        self.assertIn(
            "runner_temp",
            self.workflow_lower,
            "the generated third-party lock should live in the runner temp area",
        )

    def test_wheel_is_built_checked_and_installed_into_fresh_environments(
        self,
    ) -> None:
        self.assertRegex(self.workflow_lower, r"python\s+-m\s+build\b")
        self.assertRegex(self.workflow_lower, r"\btwine\s+check\b")
        self.assertIn("dist/", self.workflow_lower)
        self.assertIn(".whl", self.workflow_lower)
        self.assertGreaterEqual(
            len(re.findall(r"\bpython(?:3)?\s+-m\s+venv\b", self.workflow_lower)),
            2,
            "base and MCP-extra wheel smokes need isolated environments",
        )
        self.assertGreaterEqual(
            len(re.findall(r"\bpip\s+install\b", self.workflow_lower)),
            2,
        )

    def test_base_and_mcp_extra_wheel_smokes_cover_entrypoints_and_data_files(
        self,
    ) -> None:
        normalized_names = re.sub(r"[^a-z0-9]+", "-", self.workflow_lower)
        self.assertIn("base-no-mcp", normalized_names)
        self.assertIn("extra-mcp", normalized_names)
        self.assertRegex(
            self.workflow,
            r"find_spec\s*\(\s*[\"']mcp[\"']\s*\)",
        )
        self.assertIn("[mcp]", self.workflow_lower)
        self.assertRegex(self.workflow_lower, r"\bshared-mind\s+--help\b")
        self.assertRegex(self.workflow_lower, r"\bshared-mind-mcp\s+--help\b")
        self.assertIn("share/shared-mind/contracts", self.workflow)
        for contract in (
            "shared-mind-kernel.schema.v1.json",
            "shared-mind-read.schema.v1.json",
            "shared-mind-product.schema.v1.json",
            "product-conformance-fixtures.v1.json",
            "atlas-predicate-registry.v1.json",
        ):
            with self.subTest(packaged_contract=contract):
                self.assertIn(contract, self.workflow)

    def test_ci_retains_coverage_test_and_wheel_evidence(self) -> None:
        upload_action = (
            "actions/upload-artifact@"
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )
        self.assertIn(upload_action, self.workflow)
        self.assertRegex(self.workflow_lower, r"coverage\s+xml\b")
        self.assertIn(".xml", self.workflow_lower)
        self.assertIn("unittest", self.workflow_lower)
        self.assertIn(".log", self.workflow_lower)
        self.assertIn("dist/*.whl", self.workflow_lower)
        self.assertIn("release-manifest.txt", self.workflow_lower)
        self.assertRegex(self.workflow_lower, r"\bsha256sum\b")
        self.assertRegex(
            self.workflow_lower,
            r"if-no-files-found:\s*warn",
        )


if __name__ == "__main__":
    unittest.main()
