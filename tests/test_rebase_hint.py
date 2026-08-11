from __future__ import annotations

import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared_mind import Kernel, Receipt
from shared_mind.canonical import sha256_json


ROOT = Path(__file__).resolve().parents[1]


class RebaseHintAdvisoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "kernel.sqlite3"
        self.registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text()
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text()
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        self.kernel = Kernel(self.database, self.registry)
        self.kernel.register_source(
            self.objects["source_revision_postgresql"],
            (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes(),
        )
        self.postgresql_receipt = self._commit(
            self.objects["assert_postgresql_proposal"], "COMMITTED"
        )
        self.mysql_receipt = self._commit(
            self.objects["assert_mysql_same_interval_proposal"], "FACT_CONFLICT"
        )
        self.conflict_id = self.mysql_receipt.conflict_ids[0]
        self.decision_receipt = self._commit(
            self.objects["record_decision_proposal"], "COMMITTED"
        )
        self._commit(self.objects["open_question_proposal"], "COMMITTED")
        self._commit(self.objects["create_work_item_proposal"], "COMMITTED")

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_non_transaction_conflict_outcomes_have_no_rebase_hint(self) -> None:
        invalid = self._unique(
            self.objects["update_work_item_status_proposal"], "invalid"
        )
        del invalid["idempotency_key"]
        invalid_receipt = self.kernel.commit(invalid)
        self.assertEqual("VALIDATION_ERROR", invalid_receipt.outcome)
        before = self._advisory_snapshot()

        results = (
            self._build_hint(
                self.objects["assert_postgresql_proposal"], self.postgresql_receipt
            ),
            self._build_hint(
                self.objects["assert_mysql_same_interval_proposal"],
                self.mysql_receipt,
            ),
            self._build_hint(invalid, invalid_receipt),
        )

        self.assertEqual((None, None, None), results)
        self.assertEqual(before, self._advisory_snapshot())

    def test_stale_claim_version_reports_current_version_and_status(self) -> None:
        self._advance_claim()
        proposal = self._unique(
            self.objects["stale_supersede_proposal"], "claim-version"
        )
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")

        self._assert_hint(
            proposal,
            receipt,
            path="$.reads[0].expected_version",
            expected=1,
            actual=2,
            aggregate_type="CLAIM",
            aggregate_id="claim_atlas_postgresql_001",
            actual_state={"version": 2, "status": "SUPERSEDED"},
            replacements={
                "$.reads[0].expected_version": 2,
                "$.guards[0].expected_status": "SUPERSEDED",
                "$.guards[1].expected_version": 2,
            },
        )

    def test_stale_claim_status_reports_current_version_and_status(self) -> None:
        self._advance_claim()
        proposal = self._unique(
            self.objects["stale_supersede_proposal"], "claim-status"
        )
        proposal["reads"][0]["expected_version"] = 2
        proposal["guards"][1]["expected_version"] = 2
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")

        self._assert_hint(
            proposal,
            receipt,
            path="$.guards[0].expected_status",
            expected="ACTIVE",
            actual="SUPERSEDED",
            aggregate_type="CLAIM",
            aggregate_id="claim_atlas_postgresql_001",
            actual_state={"version": 2, "status": "SUPERSEDED"},
            replacements={
                "$.reads[0].expected_version": 2,
                "$.guards[0].expected_status": "SUPERSEDED",
                "$.guards[1].expected_version": 2,
            },
        )

    def test_stale_active_set_read_reports_current_digest(self) -> None:
        family_key, digest = self._active_family_state()
        proposal = self._unique(
            self.objects["stale_supersede_proposal"], "active-set-read"
        )
        proposal["reads"].insert(
            0,
            {
                "kind": "COLLECTION",
                "family_key": family_key,
                "expected_digest": "sha256:" + "0" * 64,
            },
        )
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")

        self._assert_hint(
            proposal,
            receipt,
            path="$.reads[0].expected_digest",
            expected="sha256:" + "0" * 64,
            actual=digest,
            aggregate_type="CLAIM_COLLECTION",
            aggregate_id=family_key,
            actual_state={"digest": digest, "member_count": 2},
            replacements={"$.reads[0].expected_digest": digest},
        )

    def test_stale_active_set_guard_reports_current_digest(self) -> None:
        family_key, digest = self._active_family_state()
        proposal = self._unique(
            self.objects["stale_supersede_proposal"], "active-set-guard"
        )
        proposal["guards"].insert(
            0,
            {
                "op": "ACTIVE_SET_DIGEST_EQ",
                "family_key": family_key,
                "expected_digest": "sha256:" + "0" * 64,
            },
        )
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")

        self._assert_hint(
            proposal,
            receipt,
            path="$.guards[0].expected_digest",
            expected="sha256:" + "0" * 64,
            actual=digest,
            aggregate_type="CLAIM_COLLECTION",
            aggregate_id=family_key,
            actual_state={"digest": digest, "member_count": 2},
            replacements={"$.guards[0].expected_digest": digest},
        )

    def test_stale_conflict_version_reports_current_state(self) -> None:
        conflict = self._advance_conflict()
        proposal = self._unique(
            self._resolve_proposal(conflict, expected_version=1), "conflict-version"
        )
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")

        self._assert_hint(
            proposal,
            receipt,
            path="$.reads[0].expected_version",
            expected=1,
            actual=2,
            aggregate_type="CONFLICT",
            aggregate_id=self.conflict_id,
            actual_state={
                "version": 2,
                "status": "RESOLVED",
                "member_digest": conflict["member_digest"],
            },
            replacements={"$.reads[0].expected_version": 2},
        )

    def test_stale_conflict_status_reports_current_state(self) -> None:
        conflict = self._advance_conflict()
        proposal = self._unique(
            self._resolve_proposal(conflict, expected_version=2), "conflict-status"
        )
        proposal["guards"] = [
            {
                "op": "CONFLICT_STATUS_EQ",
                "conflict_id": self.conflict_id,
                "expected_status": "OPEN",
            }
        ]
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")

        self._assert_hint(
            proposal,
            receipt,
            path="$.guards[0].expected_status",
            expected="OPEN",
            actual="RESOLVED",
            aggregate_type="CONFLICT",
            aggregate_id=self.conflict_id,
            actual_state={
                "version": 2,
                "status": "RESOLVED",
                "member_digest": conflict["member_digest"],
            },
            replacements={
                "$.reads[0].expected_version": 2,
                "$.guards[0].expected_status": "RESOLVED",
            },
        )

    def test_stale_conflict_operation_digest_reports_current_state(self) -> None:
        conflict = self._conflict_row()
        proposal = self._unique(
            self._resolve_proposal(conflict, expected_version=1),
            "conflict-operation-digest",
        )
        proposal["operations"][0]["expected_member_digest"] = "sha256:" + "0" * 64
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")

        self._assert_hint(
            proposal,
            receipt,
            path="$.operations[0].expected_member_digest",
            expected="sha256:" + "0" * 64,
            actual=conflict["member_digest"],
            aggregate_type="CONFLICT",
            aggregate_id=self.conflict_id,
            actual_state={
                "version": 1,
                "status": "OPEN",
                "member_digest": conflict["member_digest"],
            },
            replacements={
                "$.operations[0].expected_member_digest": conflict["member_digest"]
            },
        )

    def test_stale_conflict_guard_digest_reports_current_state(self) -> None:
        conflict = self._conflict_row()
        proposal = self._unique(
            self._resolve_proposal(conflict, expected_version=1),
            "conflict-guard-digest",
        )
        proposal["guards"] = [
            {
                "op": "CONFLICT_MEMBER_DIGEST_EQ",
                "conflict_id": self.conflict_id,
                "expected_digest": "sha256:" + "0" * 64,
            }
        ]
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")

        self._assert_hint(
            proposal,
            receipt,
            path="$.guards[0].expected_digest",
            expected="sha256:" + "0" * 64,
            actual=conflict["member_digest"],
            aggregate_type="CONFLICT",
            aggregate_id=self.conflict_id,
            actual_state={
                "version": 1,
                "status": "OPEN",
                "member_digest": conflict["member_digest"],
            },
            replacements={"$.guards[0].expected_digest": conflict["member_digest"]},
        )

    def test_stale_decision_version_reports_current_state(self) -> None:
        self._advance_decision()
        proposal = self._unique(
            self.objects["supersede_decision_proposal"], "decision-version"
        )
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")
        self._assert_continuity_hint(
            proposal,
            receipt,
            path="$.reads[0].expected_version",
            expected=1,
            actual=2,
            aggregate_type="DECISION_RECORD",
            aggregate_id="decision_atlas_database_strategy_001",
            status="SUPERSEDED",
        )

    def test_stale_decision_status_reports_current_state(self) -> None:
        self._advance_decision()
        proposal = self._unique(
            self.objects["supersede_decision_proposal"], "decision-status"
        )
        self._set_current_version(proposal, 2)
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")
        self._assert_continuity_hint(
            proposal,
            receipt,
            path="$.guards[0].expected_status",
            expected="ACTIVE",
            actual="SUPERSEDED",
            aggregate_type="DECISION_RECORD",
            aggregate_id="decision_atlas_database_strategy_001",
            status="SUPERSEDED",
        )

    def test_stale_question_version_reports_current_state(self) -> None:
        self._advance_question()
        proposal = self._unique(
            self.objects["drop_question_proposal"], "question-version"
        )
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")
        self._assert_continuity_hint(
            proposal,
            receipt,
            path="$.reads[0].expected_version",
            expected=1,
            actual=2,
            aggregate_type="OPEN_QUESTION",
            aggregate_id="question_atlas_cutover_window_001",
            status="DROPPED",
        )

    def test_stale_question_status_reports_current_state(self) -> None:
        self._advance_question()
        proposal = self._unique(
            self.objects["drop_question_proposal"], "question-status"
        )
        self._set_current_version(proposal, 2)
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")
        self._assert_continuity_hint(
            proposal,
            receipt,
            path="$.guards[0].expected_status",
            expected="OPEN",
            actual="DROPPED",
            aggregate_type="OPEN_QUESTION",
            aggregate_id="question_atlas_cutover_window_001",
            status="DROPPED",
        )

    def test_stale_work_item_version_reports_current_state(self) -> None:
        self._advance_work_item()
        proposal = self._unique(
            self.objects["update_work_item_status_proposal"], "work-version"
        )
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")
        self._assert_continuity_hint(
            proposal,
            receipt,
            path="$.reads[0].expected_version",
            expected=1,
            actual=2,
            aggregate_type="WORK_ITEM",
            aggregate_id="workitem_prepare_atlas_migration_001",
            status="BLOCKED",
        )

    def test_stale_work_item_status_reports_current_state(self) -> None:
        self._advance_work_item()
        proposal = self._unique(
            self.objects["update_work_item_status_proposal"], "work-status"
        )
        self._set_current_version(proposal, 2)
        receipt = self._commit(proposal, "TRANSACTION_CONFLICT")
        self._assert_continuity_hint(
            proposal,
            receipt,
            path="$.guards[0].expected_status",
            expected="TODO",
            actual="BLOCKED",
            aggregate_type="WORK_ITEM",
            aggregate_id="workitem_prepare_atlas_migration_001",
            status="BLOCKED",
        )

    def _assert_continuity_hint(
        self,
        proposal: dict[str, Any],
        receipt: Receipt,
        *,
        path: str,
        expected: Any,
        actual: Any,
        aggregate_type: str,
        aggregate_id: str,
        status: str,
    ) -> None:
        self._assert_hint(
            proposal,
            receipt,
            path=path,
            expected=expected,
            actual=actual,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            actual_state={"version": 2, "status": status},
            replacements={
                "$.reads[0].expected_version": 2,
                "$.guards[0].expected_status": status,
                "$.guards[1].expected_version": 2,
            },
        )

    def _assert_hint(
        self,
        proposal: dict[str, Any],
        receipt: Receipt,
        *,
        path: str,
        expected: Any,
        actual: Any,
        aggregate_type: str,
        aggregate_id: str,
        actual_state: dict[str, Any],
        replacements: dict[str, Any],
    ) -> None:
        self.assertEqual("TRANSACTION_CONFLICT", receipt.outcome)
        proposal_before = copy.deepcopy(proposal)
        before = self._advisory_snapshot()

        hint = self._build_hint(proposal, receipt)

        self.assertEqual(proposal_before, proposal)
        self.assertEqual(before, self._advisory_snapshot())
        self.assertEqual(
            {
                "hint_version",
                "advisory",
                "proposal_id",
                "receipt_id",
                "reason_code",
                "observed_state_root",
                "observed_ledger_head",
                "failed_precondition",
                "replacement_preconditions",
                "safe_to_auto_apply",
                "recommended_action",
            },
            set(hint),
        )
        self.assertEqual("rebase-hint@1", hint["hint_version"])
        self.assertTrue(hint["advisory"])
        self.assertEqual(proposal["proposal_id"], hint["proposal_id"])
        self.assertEqual(receipt.document["receipt_id"], hint["receipt_id"])
        self.assertEqual(receipt.reason_codes[0], hint["reason_code"])
        self.assertEqual(receipt.state_root, hint["observed_state_root"])
        self.assertEqual(before[1], hint["observed_ledger_head"])
        self.assertFalse(hint["safe_to_auto_apply"])
        self.assertEqual("REVIEW_AND_REBUILD", hint["recommended_action"])
        failed = hint["failed_precondition"]
        self.assertEqual(path, failed["path"])
        self.assertEqual(expected, failed["expected"])
        self.assertEqual(actual, failed["actual"])
        self.assertEqual(aggregate_type, failed["aggregate_type"])
        self.assertEqual(aggregate_id, failed["aggregate_id"])
        self.assertEqual(actual_state, failed["actual_state"])
        replacement_by_path = {
            item["path"]: item["value"] for item in hint["replacement_preconditions"]
        }
        for replacement_path, value in replacements.items():
            self.assertEqual(value, replacement_by_path[replacement_path])

    def _build_hint(
        self, proposal: dict[str, Any], receipt: Receipt
    ) -> dict[str, Any] | None:
        module = importlib.import_module("shared_mind.rebase")
        return module.build_rebase_hint(self.kernel, proposal, receipt)

    def _advance_claim(self) -> None:
        self._commit(self.objects["stale_supersede_proposal"], "FACT_CONFLICT")

    def _advance_conflict(self) -> dict[str, Any]:
        conflict = self._conflict_row()
        self._commit(self._resolve_proposal(conflict, expected_version=1), "COMMITTED")
        return self._conflict_row()

    def _advance_decision(self) -> None:
        self._commit(self.objects["supersede_decision_proposal"], "COMMITTED")

    def _advance_question(self) -> None:
        self._commit(self.objects["drop_question_proposal"], "COMMITTED")

    def _advance_work_item(self) -> None:
        self._commit(self.objects["update_work_item_status_proposal"], "COMMITTED")

    def _resolve_proposal(
        self, conflict: dict[str, Any], *, expected_version: int
    ) -> dict[str, Any]:
        members = json.loads(conflict["members"])
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["proposal_id"] = "proposal_resolve_rebase_hint_001"
        proposal["idempotency_key"] = "resolve-rebase-hint-001"
        proposal["proposer"] = {
            "actor_id": "human:maintainer",
            "actor_type": "HUMAN",
        }
        proposal["reads"] = [
            {
                "kind": "AGGREGATE",
                "aggregate_type": "CONFLICT",
                "aggregate_id": self.conflict_id,
                "expected_version": expected_version,
            }
        ]
        proposal["guards"] = []
        proposal["operations"] = [
            {
                "op_id": "operation_resolve_rebase_hint_001",
                "op": "RESOLVE_CONFLICT",
                "conflict_id": self.conflict_id,
                "expected_member_digest": conflict["member_digest"],
                "resolution": {
                    "resolver": proposal["proposer"],
                    "authority_policy_version": "conflict-authority@1",
                    "selected_claim_ids": ["claim_atlas_postgresql_001"],
                    "rejected_claim_ids": [
                        item
                        for item in members
                        if item != "claim_atlas_postgresql_001"
                    ],
                    "rationale": "Use the primary runbook statement.",
                    "evidence_link_ids": ["evidence_atlas_postgresql_001"],
                    "decided_at": "2026-08-01T00:07:00Z",
                    "resolution_epoch": conflict["episode"],
                },
            }
        ]
        return proposal

    def _active_family_state(self) -> tuple[str, str]:
        conflict = self._conflict_row()
        family_key = str(conflict["family_key"])
        members = []
        for row in self.kernel.connection.execute(
            "SELECT claim_id, proposition_hash, version, proposition "
            "FROM claims WHERE status = 'ACTIVE' ORDER BY claim_id"
        ):
            proposition = json.loads(row["proposition"])
            if proposition["predicate"] == "deployment.database_engine@1":
                members.append(
                    {
                        "claim_id": row["claim_id"],
                        "proposition_hash": row["proposition_hash"],
                        "version": row["version"],
                    }
                )
        return family_key, sha256_json(members)

    def _conflict_row(self) -> dict[str, Any]:
        row = self.kernel.connection.execute(
            "SELECT * FROM conflicts WHERE conflict_id = ?", (self.conflict_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    @staticmethod
    def _set_current_version(proposal: dict[str, Any], version: int) -> None:
        proposal["reads"][0]["expected_version"] = version
        proposal["guards"][1]["expected_version"] = version

    @staticmethod
    def _unique(proposal: dict[str, Any], suffix: str) -> dict[str, Any]:
        result = copy.deepcopy(proposal)
        normalized = suffix.replace("-", "_")
        result["proposal_id"] = f"proposal_rebase_{normalized}_001"
        result["idempotency_key"] = f"rebase-{suffix}-001"
        return result

    def _commit(self, proposal: dict[str, Any], expected: str) -> Receipt:
        receipt = self.kernel.commit(copy.deepcopy(proposal))
        self.assertEqual(expected, receipt.outcome, receipt.reason_codes)
        return receipt

    def _advisory_snapshot(self) -> tuple[int, str | None, str, int]:
        head = self.kernel.connection.execute(
            "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        ledger_count = int(
            self.kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        )
        receipt_count = int(
            self.kernel.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        )
        return (
            ledger_count,
            None if head is None else str(head["entry_hash"]),
            self.kernel.state_root(),
            receipt_count,
        )


if __name__ == "__main__":
    unittest.main()
