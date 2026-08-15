from __future__ import annotations

import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 test environment only
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]


class PackageMetadataTest(unittest.TestCase):
    def test_distribution_is_explicitly_proprietary_and_nonmodifiable(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            metadata = tomllib.load(handle)

        license_path = ROOT / "LICENSE"
        self.assertTrue(license_path.is_file())
        license_text = license_path.read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(
            "LicenseRef-Proprietary",
            metadata["project"]["license"],
        )
        self.assertEqual(["LICENSE"], metadata["project"]["license-files"])
        self.assertIn("setuptools>=77.0.3", metadata["build-system"]["requires"])
        self.assertIn("SHARED MIND PROPRIETARY SOURCE LICENSE", license_text)
        self.assertIn("Copyright (c) 2026 ArthurCore", license_text)
        self.assertIn("All rights reserved", license_text)
        self.assertIn("No license or permission is granted", license_text)
        self.assertIn("modify, adapt, translate", license_text)
        self.assertIn("sell, license, monetize", license_text)
        self.assertIn("Separate Paid Commercial License", license_text)
        self.assertIn("GitHub Terms of Service", license_text)
        self.assertIn("## License", readme)
        self.assertIn("LicenseRef-Proprietary", readme)
        self.assertIn("Modification and commercial use are prohibited", readme)

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
