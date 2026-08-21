from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 test environment only
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]


class PackageMetadataTest(unittest.TestCase):
    def test_distribution_uses_the_canonical_apache_2_license(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            metadata = tomllib.load(handle)

        license_path = ROOT / "LICENSE"
        notice_path = ROOT / "NOTICE"
        self.assertTrue(license_path.is_file())
        self.assertTrue(notice_path.is_file())
        license_text = license_path.read_text(encoding="utf-8")
        notice_text = notice_path.read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(
            "Apache-2.0",
            metadata["project"]["license"],
        )
        self.assertEqual(
            ["LICENSE", "NOTICE"], metadata["project"]["license-files"]
        )
        self.assertIn("setuptools>=77.0.3", metadata["build-system"]["requires"])
        self.assertEqual(
            "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
            hashlib.sha256(license_path.read_bytes()).hexdigest(),
        )
        self.assertEqual("Apache License", license_text.splitlines()[1].strip())
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn(
            "Grant of Patent License",
            license_text,
        )
        self.assertIn(
            "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
            license_text,
        )
        self.assertIn("END OF TERMS AND CONDITIONS", license_text)
        self.assertEqual("Shared Mind\nCopyright 2026 ArthurCore\n", notice_text)
        self.assertIn("## License", readme)
        self.assertIn("[Apache License 2.0](LICENSE) (`Apache-2.0`)", readme)
        self.assertIn("[NOTICE](NOTICE)", readme)

    def test_readme_documents_the_current_operator_workflow(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for command_or_route in (
            "uv tool install --editable '.[mcp]'",
            "shared-mind setup --install-hooks",
            "shared-mind session start",
            "shared-mind session prompt",
            "shared-mind resume",
            "shared-mind-product observe start",
            "shared-mind-product observe append",
            "shared-mind-product observe finalize",
            "shared-mind-product observe prune",
            "shared-mind-web --workspace",
            "/observations",
            "/review",
            "X-Shared-Mind-CSRF-Token",
            "shared-mind-product review-queue",
            "shared-mind-product draft commit",
            "shared-mind-product draft reject",
        ):
            with self.subTest(command_or_route=command_or_route):
                self.assertIn(command_or_route, readme)

    def test_installed_package_exposes_cli_and_default_contracts(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)

        scripts = project["project"]["scripts"]
        self.assertEqual("shared_mind.cli:main", scripts["shared-mind"])
        self.assertEqual("shared_mind.mcp_server:main", scripts["shared-mind-mcp"])
        self.assertEqual(
            "shared_mind.product_cli:main", scripts["shared-mind-product"]
        )
        self.assertEqual(
            "shared_mind.product_mcp_server:main",
            scripts["shared-mind-product-mcp"],
        )
        self.assertEqual("shared_mind.web_control:main", scripts["shared-mind-web"])
        packaged_contracts = project["tool"]["setuptools"]["data-files"][
            "share/shared-mind/contracts"
        ]
        self.assertIn(
            "contracts/shared-mind-kernel.schema.v1.json", packaged_contracts
        )
        self.assertIn(
            "contracts/atlas-predicate-registry.v1.json", packaged_contracts
        )
        self.assertIn(
            "contracts/shared-mind-product.schema.v1.json", packaged_contracts
        )
        self.assertIn(
            "contracts/product-conformance-fixtures.v1.json", packaged_contracts
        )


if __name__ == "__main__":
    unittest.main()
