from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json


ROOT = Path(__file__).resolve().parents[1]


class IntegritySemanticsConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )
        fixtures = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        cls.objects = {
            item["name"]: item["object"] for item in fixtures["typed_objects"]
        }
        cls.content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_nfr_004_verifier_recomputes_conflict_member_digest(self) -> None:
        kernel = self._kernel("member-digest.sqlite3")
        self.addCleanup(kernel.close)
        sequence, conflict = self._commit_conflict(kernel)

        self._forge_conflict_field(
            kernel,
            sequence,
            conflict,
            field="member_digest",
            forged_value="sha256:" + "0" * 64,
        )

        result = kernel.verify_ledger()

        self.assertFalse(result["valid"])
        self.assertIn(
            f"CONFLICT_MEMBER_DIGEST_MISMATCH:{sequence}", result["errors"]
        )

    def test_nfr_004_verifier_recomputes_conflict_family_key(self) -> None:
        kernel = self._kernel("family-key.sqlite3")
        self.addCleanup(kernel.close)
        sequence, conflict = self._commit_conflict(kernel)

        self._forge_conflict_field(
            kernel,
            sequence,
            conflict,
            field="family_key",
            forged_value="sha256:" + "f" * 64,
        )

        result = kernel.verify_ledger()

        self.assertFalse(result["valid"])
        self.assertIn(
            f"CONFLICT_FAMILY_KEY_MISMATCH:{sequence}", result["errors"]
        )

    def test_fr_041_hash_consistent_malformed_event_is_a_structured_error(self) -> None:
        kernel = self._kernel("malformed-event.sqlite3")
        self.addCleanup(kernel.close)
        self.assertEqual("COMMITTED", kernel.commit(self._source_proposal()).outcome)
        row = kernel.connection.execute("SELECT * FROM ledger WHERE seq = 1").fetchone()
        self.assertIsNotNone(row)
        malformed_events = [{"event_type": "SOURCE_REVISION_REGISTERED"}]
        entry_hash = self._entry_hash(row, malformed_events, row["state_root"])
        with kernel._authorized_writes():
            self._allow_forensic_ledger_rewrite(kernel)
            kernel.connection.execute(
                "UPDATE ledger SET events = ?, entry_hash = ? WHERE seq = 1",
                (canonical_json(malformed_events), entry_hash),
            )

        result = kernel.verify_ledger()

        self.assertFalse(result["valid"])
        self.assertIn("LEDGER_EVENT_SCHEMA_INVALID:1", result["errors"])

    def test_fr_022_conflicts_follow_registry_rules_instead_of_hard_coding(self) -> None:
        registry = copy.deepcopy(self.registry)
        predicate = next(
            item
            for item in registry["predicates"]
            if item["key"] == "deployment.database_engine@1"
        )
        predicate["conflict_rules"] = [
            rule
            for rule in predicate["conflict_rules"]
            if rule["kind"] != "EXCLUSIVE_OBJECT"
        ]
        kernel = self._kernel("registry-rules.sqlite3", registry=registry)
        self.addCleanup(kernel.close)
        self.assertEqual("COMMITTED", kernel.commit(self._source_proposal()).outcome)
        first = kernel.commit(self.objects["assert_postgresql_proposal"])

        second = kernel.commit(self.objects["assert_mysql_same_interval_proposal"])

        self.assertEqual("COMMITTED", first.outcome)
        self.assertEqual("COMMITTED", second.outcome)
        self.assertEqual((), second.conflict_ids)
        self.assertEqual(
            0,
            kernel.connection.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0],
        )

    def _kernel(
        self, filename: str, *, registry: dict[str, Any] | None = None
    ) -> Kernel:
        return Kernel(
            Path(self.temp.name) / filename,
            copy.deepcopy(registry if registry is not None else self.registry),
        )

    def _source_proposal(self) -> dict[str, Any]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["proposal_id"] = "proposal_integrity_source_001"
        proposal["idempotency_key"] = "integrity-source-001"
        source = copy.deepcopy(self.objects["source_revision_postgresql"])
        source["blob_ref"] = (
            f"data:{source['media_type']};base64,"
            + base64.b64encode(self.content).decode("ascii")
        )
        proposal["operations"] = [
            {
                "op_id": "operation_integrity_source_001",
                "op": "REGISTER_SOURCE_REVISION",
                "source_revision": source,
            }
        ]
        return proposal

    def _commit_conflict(self, kernel: Kernel) -> tuple[int, dict[str, Any]]:
        self.assertEqual("COMMITTED", kernel.commit(self._source_proposal()).outcome)
        self.assertEqual(
            "COMMITTED",
            kernel.commit(self.objects["assert_postgresql_proposal"]).outcome,
        )
        receipt = kernel.commit(self.objects["assert_mysql_same_interval_proposal"])
        self.assertEqual("FACT_CONFLICT", receipt.outcome)
        self.assertIsNotNone(receipt.ledger_seq)
        row = kernel.connection.execute(
            "SELECT events FROM ledger WHERE seq = ?", (receipt.ledger_seq,)
        ).fetchone()
        self.assertIsNotNone(row)
        conflict = next(
            event["conflict"]
            for event in json.loads(row["events"])
            if event["event_type"] == "CONFLICT_OPENED"
        )
        return int(receipt.ledger_seq), conflict

    def _forge_conflict_field(
        self,
        kernel: Kernel,
        sequence: int,
        conflict: dict[str, Any],
        *,
        field: str,
        forged_value: str,
    ) -> None:
        row = kernel.connection.execute(
            "SELECT * FROM ledger WHERE seq = ?", (sequence,)
        ).fetchone()
        self.assertIsNotNone(row)
        events = json.loads(row["events"])
        conflict_event = next(
            event for event in events if event["event_type"] == "CONFLICT_OPENED"
        )
        conflict_event["conflict"][field] = forged_value
        with kernel._authorized_writes():
            kernel.connection.execute(
                f"UPDATE conflicts SET {field} = ? WHERE conflict_id = ?",
                (forged_value, conflict["conflict_id"]),
            )
            forged_root = kernel.state_root()
            entry_hash = self._entry_hash(row, events, forged_root)
            self._allow_forensic_ledger_rewrite(kernel)
            kernel.connection.execute(
                "UPDATE ledger SET events = ?, state_root = ?, entry_hash = ? WHERE seq = ?",
                (canonical_json(events), forged_root, entry_hash, sequence),
            )

    @staticmethod
    def _entry_hash(row: Any, events: list[dict[str, Any]], state_root: str) -> str:
        proposal = json.loads(row["proposal"])
        envelope = Kernel._ledger_envelope(
            seq=int(row["seq"]),
            prev_hash=row["prev_hash"],
            proposal_hash=row["proposal_hash"],
            pre_state_root=row["pre_state_root"],
            post_state_root=state_root,
            versions=proposal["versions"],
            events=events,
            committed_at=row["committed_at"],
        )
        return sha256_json(envelope)

    @staticmethod
    def _allow_forensic_ledger_rewrite(kernel: Kernel) -> None:
        kernel.connection.execute("DROP TRIGGER ledger_no_update")


if __name__ == "__main__":
    unittest.main()
