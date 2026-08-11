from __future__ import annotations

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
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", registry)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def proposal(self, name: str) -> dict[str, object]:
        return copy.deepcopy(self.objects[name])

    def test_continuity_operations_are_atomic_ledger_mutations(self) -> None:
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

        receipt = self.kernel.commit(stale)

        self.assertEqual("TRANSACTION_CONFLICT", receipt.outcome)
        self.assertEqual(("QUESTION_VERSION_MISMATCH",), receipt.reason_codes)
        self.assertEqual(2, self.kernel.connection.execute(
            "SELECT COUNT(*) FROM ledger"
        ).fetchone()[0])
        self.assertEqual(before, self.kernel.state_root())


if __name__ == "__main__":
    unittest.main()
