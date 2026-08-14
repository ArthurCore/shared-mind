from __future__ import annotations

import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 test environment only
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]


class PackageMetadataTest(unittest.TestCase):
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
