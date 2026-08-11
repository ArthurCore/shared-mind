from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind import Kernel
from shared_mind.continuity import state_rows


ROOT = Path(__file__).resolve().parents[1]


class ContinuityKernelIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )
        fixtures = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.objects = {
            item["name"]: item["object"] for item in fixtures["typed_objects"]
        }
        self.content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", registry)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def proposal(self, name: str) -> dict[str, object]:
        return copy.deepcopy(self.objects[name])

    def test_continuity_operations_are_atomic_ledger_mutations(self) -> None:
        self._seed_canonical_references()
        empty_root = self.kernel.state_root()

        recorded = self.kernel.commit(self.proposal("record_decision_proposal"))

        self.assertEqual("COMMITTED", recorded.outcome)
        self.assertNotEqual(empty_root, recorded.state_root)
        self.assertEqual(recorded.state_root, self.kernel.state_root())
        rows = state_rows(self.kernel.connection)
        self.assertEqual("ACTIVE", rows["decision_records"][0]["status"])

        missing_read = self.proposal("supersede_decision_proposal")
        missing_read["proposal_id"] = "proposal_supersede_without_read_001"
        missing_read["idempotency_key"] = "supersede-without-read-001"
        missing_read["reads"] = []
        ledger_before = self.kernel.connection.execute(
            "SELECT COUNT(*) FROM ledger"
        ).fetchone()[0]
        root_before = self.kernel.state_root()

        rejected = self.kernel.commit(missing_read)

        self.assertEqual("VALIDATION_ERROR", rejected.outcome)
        self.assertEqual(
            ("MISSING_REQUIRED_DECISION_READ",), rejected.reason_codes
        )
        self.assertEqual(
            ledger_before,
            self.kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
        )
        self.assertEqual(root_before, self.kernel.state_root())

        superseded = self.kernel.commit(self.proposal("supersede_decision_proposal"))

        self.assertEqual("COMMITTED", superseded.outcome)
        decisions = state_rows(self.kernel.connection)["decision_records"]
        self.assertEqual(["SUPERSEDED", "ACTIVE"], [row["status"] for row in decisions])

    def test_continuity_lifecycle_replays_to_identical_state(self) -> None:
        self._seed_canonical_references()
        for name in (
            "record_decision_proposal",
            "supersede_decision_proposal",
            "open_question_proposal",
            "answer_question_proposal",
            "create_work_item_proposal",
            "update_work_item_status_proposal",
        ):
            with self.subTest(proposal=name):
                receipt = self.kernel.commit(self.proposal(name))
                self.assertEqual("COMMITTED", receipt.outcome, receipt.reason_codes)

        expected_rows = state_rows(self.kernel.connection)
        expected_root = self.kernel.state_root()
        replayed = self.kernel.replay(Path(self.temp.name) / "replayed.sqlite3")
        self.addCleanup(replayed.close)

        self.assertEqual(expected_rows, state_rows(replayed.connection))
        self.assertEqual(expected_root, replayed.state_root())
        self.assertTrue(replayed.verify_ledger()["valid"])

    def test_stale_continuity_guard_rolls_back_without_ledger_append(self) -> None:
        self._seed_canonical_references()
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.proposal("record_decision_proposal")).outcome,
        )
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.proposal("supersede_decision_proposal")).outcome,
        )
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.proposal("open_question_proposal")).outcome,
        )
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.proposal("answer_question_proposal")).outcome,
        )
        stale = self.proposal("answer_question_proposal")
        stale["proposal_id"] = "proposal_answer_question_stale_001"
        stale["idempotency_key"] = "answer-question-stale-001"
        before = self.kernel.state_root()
        ledger_before = self.kernel.connection.execute(
            "SELECT COUNT(*) FROM ledger"
        ).fetchone()[0]

        receipt = self.kernel.commit(stale)

        self.assertEqual("TRANSACTION_CONFLICT", receipt.outcome)
        self.assertEqual(("QUESTION_VERSION_MISMATCH",), receipt.reason_codes)
        self.assertEqual(ledger_before, self.kernel.connection.execute(
            "SELECT COUNT(*) FROM ledger"
        ).fetchone()[0])
        self.assertEqual(before, self.kernel.state_root())

    def test_continuity_reference_must_resolve_before_commit(self) -> None:
        receipt = self.kernel.commit(self.proposal("record_decision_proposal"))

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("REFERENCE_NOT_FOUND",), receipt.reason_codes)
        self.assertEqual(0, self.kernel.connection.execute(
            "SELECT COUNT(*) FROM decision_records"
        ).fetchone()[0])
        self.assertEqual(0, self.kernel.connection.execute(
            "SELECT COUNT(*) FROM ledger"
        ).fetchone()[0])

    def test_continuity_reference_type_must_match(self) -> None:
        self._seed_canonical_references()
        proposal = self.proposal("record_decision_proposal")
        proposal["proposal_id"] = "proposal_decision_wrong_ref_type_001"
        proposal["idempotency_key"] = "decision-wrong-ref-type-001"
        decision = proposal["operations"][0]["decision"]
        decision["related_claim_ids"] = [
            self.objects["source_revision_postgresql"]["revision_id"]
        ]

        receipt = self.kernel.commit(proposal)

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("REFERENCE_TYPE_MISMATCH",), receipt.reason_codes)

    def test_same_proposal_forward_continuity_reference_resolves(self) -> None:
        self._seed_canonical_references()
        proposal = self.proposal("open_question_proposal")
        proposal["proposal_id"] = "proposal_forward_decision_ref_001"
        proposal["idempotency_key"] = "forward-decision-ref-001"
        question_operation = proposal["operations"][0]
        question_operation["question"]["related_objects"] = [
            {
                "record_type": "DECISION_RECORD",
                "record_id": "decision_atlas_database_strategy_001",
            }
        ]
        decision_operation = self.proposal("record_decision_proposal")["operations"][0]
        proposal["operations"] = [question_operation, decision_operation]

        receipt = self.kernel.commit(proposal)

        self.assertEqual("COMMITTED", receipt.outcome, receipt.reason_codes)
        self.assertEqual(1, self.kernel.connection.execute(
            "SELECT COUNT(*) FROM open_questions"
        ).fetchone()[0])
        self.assertEqual(1, self.kernel.connection.execute(
            "SELECT COUNT(*) FROM decision_records"
        ).fetchone()[0])

    def _seed_canonical_references(self) -> None:
        source_proposal = self.proposal("assert_postgresql_proposal")
        source_proposal["proposal_id"] = "proposal_continuity_source_001"
        source_proposal["idempotency_key"] = "continuity-source-0001"
        source = copy.deepcopy(self.objects["source_revision_postgresql"])
        source["blob_ref"] = (
            f"data:{source['media_type']};base64,"
            + base64.b64encode(self.content).decode("ascii")
        )
        source_proposal["operations"] = [
            {
                "op_id": "operation_continuity_source_001",
                "op": "REGISTER_SOURCE_REVISION",
                "source_revision": source,
            }
        ]
        for proposal in (
            source_proposal,
            self.proposal("assert_postgresql_proposal"),
            self.proposal("assert_mysql_same_interval_proposal"),
        ):
            receipt = self.kernel.commit(proposal)
            self.assertIn(receipt.outcome, {"COMMITTED", "FACT_CONFLICT"})


if __name__ == "__main__":
    unittest.main()
