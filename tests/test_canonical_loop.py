from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind import Kernel
from shared_mind.canonical import sha256_json


ROOT = Path(__file__).resolve().parents[1]


class CanonicalLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text()
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text()
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        self.content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", self.registry)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_fr_004_source_registration_is_a_replayable_proposal_operation(self) -> None:
        receipt = self.kernel.commit(self._source_proposal())

        self.assertEqual("COMMITTED", receipt.outcome)
        source = self.kernel.connection.execute(
            "SELECT document, content FROM sources WHERE revision_id = ?",
            (self.objects["source_revision_postgresql"]["revision_id"],),
        ).fetchone()
        self.assertEqual(self.content, bytes(source["content"]))
        events = json.loads(
            self.kernel.connection.execute("SELECT events FROM ledger").fetchone()[0]
        )
        self.assertEqual("SOURCE_REVISION_REGISTERED", events[0]["event_type"])
        self.assertEqual(
            self.objects["source_revision_postgresql"]["revision_id"],
            events[0]["source_revision"]["revision_id"],
        )

    def test_fr_004_source_registration_rejects_unavailable_or_wrong_content(self) -> None:
        unavailable = self._source_proposal()
        unavailable["proposal_id"] = "proposal_source_unavailable_001"
        unavailable["idempotency_key"] = "source-unavailable-001"
        unavailable["operations"][0]["source_revision"]["blob_ref"] = (
            "urn:shared-mind:blob:not-staged"
        )

        unavailable_receipt = self.kernel.commit(unavailable)

        self.assertEqual("VALIDATION_ERROR", unavailable_receipt.outcome)
        self.assertEqual(
            ("SOURCE_CONTENT_UNAVAILABLE",), unavailable_receipt.reason_codes
        )
        wrong = self._source_proposal()
        wrong["proposal_id"] = "proposal_source_wrong_hash_001"
        wrong["idempotency_key"] = "source-wrong-hash-001"
        wrong["operations"][0]["source_revision"]["blob_ref"] = (
            "data:text/plain;base64," + base64.b64encode(b"wrong").decode("ascii")
        )

        wrong_receipt = self.kernel.commit(wrong)

        self.assertEqual("VALIDATION_ERROR", wrong_receipt.outcome)
        self.assertEqual(
            ("SOURCE_CONTENT_HASH_MISMATCH",), wrong_receipt.reason_codes
        )
        self.assertEqual(0, self._count("sources"))
        self.assertEqual(0, self._count("ledger"))

    def test_fr_023_retract_requires_observed_version_and_authority(self) -> None:
        self._commit_source_and_postgresql()
        missing_read = self._retract_proposal()

        missing_receipt = self.kernel.commit(missing_read)

        self.assertEqual("VALIDATION_ERROR", missing_receipt.outcome)
        self.assertEqual(
            ("MISSING_REQUIRED_CLAIM_READ",), missing_receipt.reason_codes
        )
        unauthorized = self._retract_proposal(expected_version=1)
        unauthorized["proposal_id"] = "proposal_retract_unauthorized_001"
        unauthorized["idempotency_key"] = "retract-unauthorized-001"
        unauthorized["proposer"] = {
            "actor_id": "agent:unrelated",
            "actor_type": "AGENT",
        }

        unauthorized_receipt = self.kernel.commit(unauthorized)

        self.assertEqual("VALIDATION_ERROR", unauthorized_receipt.outcome)
        self.assertEqual(("ACTOR_NOT_AUTHORIZED",), unauthorized_receipt.reason_codes)
        self.assertEqual("ACTIVE", self._claim_row()["status"])

        accepted = self._retract_proposal(expected_version=1)
        accepted["proposal_id"] = "proposal_retract_authorized_001"
        accepted["idempotency_key"] = "retract-authorized-001"
        accepted_receipt = self.kernel.commit(accepted)

        self.assertEqual("COMMITTED", accepted_receipt.outcome)
        self.assertEqual(("RETRACTED", 2), tuple(self._claim_row()))
        event = json.loads(
            self.kernel.connection.execute(
                "SELECT events FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()[0]
        )[0]
        self.assertEqual("CLAIM_RETRACTED", event["event_type"])
        self.assertEqual(
            self.objects["assert_postgresql_proposal"]["proposer"], event["actor"]
        )

    def test_fr_025_conflict_resolve_and_reopen_preserve_episode_history(self) -> None:
        conflict_id = self._commit_conflict()
        conflict = self._conflict_row(conflict_id)
        missing_read = self._resolve_proposal(conflict)

        missing_receipt = self.kernel.commit(missing_read)

        self.assertEqual("VALIDATION_ERROR", missing_receipt.outcome)
        self.assertEqual(
            ("MISSING_REQUIRED_CONFLICT_READ",), missing_receipt.reason_codes
        )
        resolution = self._resolve_proposal(conflict, expected_version=1)
        resolution["proposal_id"] = "proposal_resolve_conflict_after_read_001"
        resolution["idempotency_key"] = "resolve-after-read-001"
        resolved_receipt = self.kernel.commit(resolution)

        self.assertEqual("COMMITTED", resolved_receipt.outcome)
        resolved = self._conflict_row(conflict_id)
        self.assertEqual("RESOLVED", resolved["status"])
        self.assertEqual(2, resolved["version"])
        self.assertEqual(1, json.loads(resolved["resolution"])["resolution_epoch"])

        third = self._third_conflicting_claim_proposal()
        reopened_receipt = self.kernel.commit(third)

        self.assertEqual("FACT_CONFLICT", reopened_receipt.outcome)
        self.assertEqual((conflict_id,), reopened_receipt.conflict_ids)
        reopened = self._conflict_row(conflict_id)
        self.assertEqual("OPEN", reopened["status"])
        self.assertEqual(2, reopened["episode"])
        self.assertEqual(3, reopened["version"])
        self.assertIsNone(reopened["resolution"])
        self.assertEqual(3, len(json.loads(reopened["members"])))
        ledger_events = [
            event
            for row in self.kernel.connection.execute("SELECT events FROM ledger ORDER BY seq")
            for event in json.loads(row["events"])
        ]
        episodes = [
            event["conflict"]["episode"]
            for event in ledger_events
            if event["event_type"] == "CONFLICT_OPENED"
            and event["conflict"]["conflict_id"] == conflict_id
        ]
        self.assertEqual([1, 2], episodes)

    def test_fr_023_stale_conflict_resolution_does_not_mutate_or_append(self) -> None:
        conflict_id = self._commit_conflict()
        conflict = self._conflict_row(conflict_id)
        accepted = self._resolve_proposal(conflict, expected_version=1)
        self.assertEqual("COMMITTED", self.kernel.commit(accepted).outcome)
        ledger_before = self._count("ledger")
        root_before = self.kernel.state_root()
        stale = self._resolve_proposal(conflict, expected_version=1)
        stale["proposal_id"] = "proposal_resolve_stale_001"
        stale["idempotency_key"] = "resolve-stale-001"

        receipt = self.kernel.commit(stale)

        self.assertEqual("TRANSACTION_CONFLICT", receipt.outcome)
        self.assertEqual(("CONFLICT_VERSION_MISMATCH",), receipt.reason_codes)
        self.assertEqual(ledger_before, self._count("ledger"))
        self.assertEqual(root_before, self.kernel.state_root())

    def test_fr_040_ledger_verify_and_replay_rebuild_identical_state(self) -> None:
        conflict_id = self._commit_conflict()
        conflict = self._conflict_row(conflict_id)
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(
                self._resolve_proposal(conflict, expected_version=1)
            ).outcome,
        )
        self.assertEqual(
            "FACT_CONFLICT", self.kernel.commit(self._third_conflicting_claim_proposal()).outcome
        )
        expected_root = self.kernel.state_root()

        verification = self.kernel.verify_ledger()
        replayed = self.kernel.replay(Path(self.temp.name) / "replayed.sqlite3")
        self.addCleanup(replayed.close)

        self.assertTrue(verification["valid"], verification["errors"])
        self.assertEqual(self._count("ledger"), verification["checked_entries"])
        self.assertEqual(expected_root, verification["state_root"])
        self.assertEqual(expected_root, replayed.state_root())
        self.assertEqual(self._count("ledger"), int(
            replayed.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        ))
        self.assertEqual(
            [tuple(row) for row in self.kernel.connection.execute(
                "SELECT conflict_id, status, episode, version, member_digest, resolution "
                "FROM conflicts ORDER BY conflict_id"
            )],
            [tuple(row) for row in replayed.connection.execute(
                "SELECT conflict_id, status, episode, version, member_digest, resolution "
                "FROM conflicts ORDER BY conflict_id"
            )],
        )

    def test_nfr_004_ledger_verifier_reports_hash_chain_corruption(self) -> None:
        self.assertEqual("COMMITTED", self.kernel.commit(self._source_proposal()).outcome)
        # Simulate a privileged forensic/owner-level tamper. Ordinary DML is
        # rejected by the append-only trigger and covered separately.
        self.kernel.connection.execute("DROP TRIGGER ledger_no_update")
        self.kernel.connection.execute(
            "UPDATE ledger SET events = ? WHERE seq = 1",
            ('[{"event_type":"CORRUPTED"}]',),
        )

        result = self.kernel.verify_ledger()

        self.assertFalse(result["valid"])
        self.assertIn("ENTRY_HASH_MISMATCH:1", result["errors"])

    def _source_proposal(self) -> dict[str, object]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["proposal_id"] = "proposal_register_source_001"
        proposal["idempotency_key"] = "register-source-001"
        source = copy.deepcopy(self.objects["source_revision_postgresql"])
        source["blob_ref"] = (
            f"data:{source['media_type']};base64,"
            + base64.b64encode(self.content).decode("ascii")
        )
        proposal["operations"] = [
            {
                "op_id": "operation_register_source_001",
                "op": "REGISTER_SOURCE_REVISION",
                "source_revision": source,
            }
        ]
        return proposal

    def _commit_source_and_postgresql(self) -> None:
        self.assertEqual("COMMITTED", self.kernel.commit(self._source_proposal()).outcome)
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.objects["assert_postgresql_proposal"]).outcome,
        )

    def _commit_conflict(self) -> str:
        self._commit_source_and_postgresql()
        receipt = self.kernel.commit(self.objects["assert_mysql_same_interval_proposal"])
        self.assertEqual("FACT_CONFLICT", receipt.outcome)
        return receipt.conflict_ids[0]

    def _retract_proposal(self, expected_version: int | None = None) -> dict[str, object]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["proposal_id"] = "proposal_retract_postgresql_001"
        proposal["idempotency_key"] = "retract-postgresql-001"
        proposal["operations"] = [
            {
                "op_id": "operation_retract_postgresql_001",
                "op": "RETRACT_CLAIM",
                "target_claim_id": "claim_atlas_postgresql_001",
                "rationale": "The assertion was withdrawn by its original actor.",
                "authority_policy_version": "claim-authority@1",
                "evidence_link_ids": ["evidence_atlas_postgresql_001"],
            }
        ]
        proposal["reads"] = [] if expected_version is None else [
            {
                "kind": "AGGREGATE",
                "aggregate_type": "CLAIM",
                "aggregate_id": "claim_atlas_postgresql_001",
                "expected_version": expected_version,
            }
        ]
        proposal["guards"] = []
        return proposal

    def _resolve_proposal(
        self, conflict: object, expected_version: int | None = None
    ) -> dict[str, object]:
        conflict_id = conflict["conflict_id"]
        members = json.loads(conflict["members"])
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["proposal_id"] = "proposal_resolve_conflict_001"
        proposal["idempotency_key"] = "resolve-conflict-001"
        proposal["proposer"] = {"actor_id": "human:maintainer", "actor_type": "HUMAN"}
        proposal["operations"] = [
            {
                "op_id": "operation_resolve_conflict_001",
                "op": "RESOLVE_CONFLICT",
                "conflict_id": conflict_id,
                "expected_member_digest": conflict["member_digest"],
                "resolution": {
                    "resolver": proposal["proposer"],
                    "authority_policy_version": "conflict-authority@1",
                    "selected_claim_ids": ["claim_atlas_postgresql_001"],
                    "rejected_claim_ids": [
                        item for item in members if item != "claim_atlas_postgresql_001"
                    ],
                    "rationale": "The runbook's primary statement is authoritative.",
                    "evidence_link_ids": ["evidence_atlas_postgresql_001"],
                    "decided_at": "2026-08-01T00:04:00Z",
                    "resolution_epoch": conflict["episode"],
                },
            }
        ]
        proposal["reads"] = [] if expected_version is None else [
            {
                "kind": "AGGREGATE",
                "aggregate_type": "CONFLICT",
                "aggregate_id": conflict_id,
                "expected_version": expected_version,
            }
        ]
        proposal["guards"] = []
        return proposal

    def _third_conflicting_claim_proposal(self) -> dict[str, object]:
        proposal = copy.deepcopy(self.objects["assert_mysql_same_interval_proposal"])
        proposal["proposal_id"] = "proposal_assert_mariadb_001"
        proposal["idempotency_key"] = "assert-mariadb-001"
        operation = proposal["operations"][0]
        operation["op_id"] = "operation_assert_mariadb_001"
        claim = operation["claim"]
        claim["claim_id"] = "claim_atlas_mariadb_001"
        claim["proposition"]["object"]["entity_id"] = "software:mariadb"
        claim["proposition_hash"] = sha256_json(claim["proposition"])
        operation["initial_evidence"][0]["claim_id"] = claim["claim_id"]
        operation["initial_evidence"][0]["evidence_link_id"] = (
            "evidence_atlas_mariadb_001"
        )
        return proposal

    def _claim_row(self):
        return self.kernel.connection.execute(
            "SELECT status, version FROM claims WHERE claim_id = ?",
            ("claim_atlas_postgresql_001",),
        ).fetchone()

    def _conflict_row(self, conflict_id: str):
        return self.kernel.connection.execute(
            "SELECT * FROM conflicts WHERE conflict_id = ?", (conflict_id,)
        ).fetchone()

    def _count(self, table: str) -> int:
        return int(
            self.kernel.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
