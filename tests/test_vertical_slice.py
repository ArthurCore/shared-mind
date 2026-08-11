from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind import Kernel


ROOT = Path(__file__).resolve().parents[1]


class AtlasVerticalSliceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        registry = json.loads((ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text())
        fixture_set = json.loads((ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text())
        self.objects = {item["name"]: item for item in fixture_set["typed_objects"]}
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", registry)
        source = self.objects["source_revision_postgresql"]["object"]
        self.kernel.register_source(source, (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes())

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_committed_fact_conflict_and_conflict_aware_read(self) -> None:
        first = self.kernel.commit(self.objects["assert_postgresql_proposal"]["object"])
        second = self.kernel.commit(self.objects["assert_mysql_same_interval_proposal"]["object"])
        self.assertEqual("COMMITTED", first.outcome)
        self.assertEqual("FACT_CONFLICT", second.outcome)
        self.assertEqual(1, len(second.conflict_ids))
        context = self.kernel.read_epistemic_context("system:atlas", "deployment.database_engine@1", "production")
        self.assertEqual(2, len(context["claims"]))
        self.assertTrue(context["has_open_conflict"])

    def test_stale_supersede_is_transaction_conflict_without_ledger_append(self) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"]["object"])
        base = copy.deepcopy(self.objects["assert_postgresql_proposal"]["object"])
        link = copy.deepcopy(base["operations"][0]["initial_evidence"][0])
        link["evidence_link_id"] = "evidence_atlas_postgresql_extra"
        attach = copy.deepcopy(base)
        attach["proposal_id"] = "proposal_attach_extra_evidence"
        attach["idempotency_key"] = "atlas-attach-extra-001"
        attach["operations"] = [{"op_id": "operation_attach_extra", "op": "ATTACH_EVIDENCE", "evidence_link": link}]
        self.assertEqual("COMMITTED", self.kernel.commit(attach).outcome)
        before = self.kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        stale = self.kernel.commit(self.objects["stale_supersede_proposal"]["object"])
        after = self.kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        self.assertEqual("TRANSACTION_CONFLICT", stale.outcome)
        self.assertEqual(("CLAIM_VERSION_MISMATCH",), stale.reason_codes)
        self.assertEqual(before, after)

    def test_idempotent_retry_does_not_append(self) -> None:
        proposal = self.objects["assert_postgresql_proposal"]["object"]
        first = self.kernel.commit(proposal)
        second = self.kernel.commit(proposal)
        self.assertEqual(first, second)
        self.assertEqual(1, self.kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])

    def test_same_idempotency_key_with_different_payload_is_rejected(self) -> None:
        proposal = self.objects["assert_postgresql_proposal"]["object"]
        self.kernel.commit(proposal)
        changed = copy.deepcopy(proposal)
        changed["proposed_at"] = "2026-08-01T00:01:01Z"
        receipt = self.kernel.commit(changed)
        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("IDEMPOTENCY_KEY_REUSE",), receipt.reason_codes)


if __name__ == "__main__":
    unittest.main()
