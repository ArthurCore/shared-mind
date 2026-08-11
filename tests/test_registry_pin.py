from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind import Kernel
from shared_mind.kernel import ValidationFailure


ROOT = Path(__file__).resolve().parents[1]


class RegistryContentPinConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        self.source_content = (
            ROOT / "contracts" / "atlas-runbook.fixture.md"
        ).read_bytes()

    def test_nfr_001_identical_registry_reopens_existing_database(self) -> None:
        database = Path(self.temp.name) / "identical.sqlite3"
        original = self._open(database, self.registry)
        receipt = original.register_source(
            self.objects["source_revision_postgresql"], self.source_content
        )
        expected_root = original.state_root()
        original.close()

        reopened = self._open(database, copy.deepcopy(self.registry))

        self.assertEqual("COMMITTED", receipt.outcome)
        self.assertEqual(expected_root, reopened.state_root())
        self.assertTrue(reopened.verify_ledger()["valid"])

    def test_fr_015_same_version_changed_registry_is_rejected_on_reopen(
        self,
    ) -> None:
        database = Path(self.temp.name) / "reopen.sqlite3"
        original = self._open(database, self.registry)
        original.register_source(
            self.objects["source_revision_postgresql"], self.source_content
        )
        original.close()
        changed = self._registry_without_exclusive_object()

        with self.assertRaises(ValidationFailure) as raised:
            Kernel(database, changed)

        self.assertEqual(
            "PREDICATE_REGISTRY_CONTENT_MISMATCH", raised.exception.code
        )

    def test_fr_040_replay_rejects_target_pinned_to_changed_registry(self) -> None:
        source_database = Path(self.temp.name) / "source.sqlite3"
        source = self._open(source_database, self.registry)
        source.register_source(
            self.objects["source_revision_postgresql"], self.source_content
        )
        replay_database = Path(self.temp.name) / "replay.sqlite3"
        changed_target = self._open(
            replay_database, self._registry_without_exclusive_object()
        )
        changed_target.close()

        with self.assertRaises(ValidationFailure) as raised:
            source.replay(replay_database)

        self.assertEqual(
            "PREDICATE_REGISTRY_CONTENT_MISMATCH", raised.exception.code
        )

    def _open(self, database: Path, registry: dict[str, object]) -> Kernel:
        kernel = Kernel(database, copy.deepcopy(registry))
        self.addCleanup(kernel.close)
        return kernel

    def _registry_without_exclusive_object(self) -> dict[str, object]:
        changed = copy.deepcopy(self.registry)
        predicate = next(
            item
            for item in changed["predicates"]
            if item["key"] == "deployment.database_engine@1"
        )
        predicate["conflict_rules"] = [
            rule
            for rule in predicate["conflict_rules"]
            if rule["kind"] != "EXCLUSIVE_OBJECT"
        ]
        self.assertEqual(self.registry["version"], changed["version"])
        return changed


if __name__ == "__main__":
    unittest.main()
