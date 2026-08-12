from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from shared_mind import Kernel, Receipt


ROOT = Path(__file__).resolve().parents[1]


class InjectedCommitFault(RuntimeError):
    """A deterministic failure raised after a materialized operation ran."""


class FaultAfterFirstOperationKernel(Kernel):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._operations_before_fault = 1

    def _apply_operation(
        self,
        operation: dict[str, Any],
        events: list[dict[str, Any]],
        conflict_ids: list[str],
    ) -> None:
        super()._apply_operation(operation, events, conflict_ids)
        self._operations_before_fault -= 1
        if self._operations_before_fault == 0:
            raise InjectedCommitFault("fault after materialized operation")


class KernelConcurrencyConformanceTest(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_fr_013_one_hundred_identical_retries_append_once(self) -> None:
        proposal = self.objects["assert_postgresql_proposal"]
        ledger_before = self._count("ledger")

        receipts = [self.kernel.commit(proposal) for _ in range(100)]

        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual("COMMITTED", receipts[0].outcome)
        self.assertEqual(ledger_before + 1, self._count("ledger"))
        self.assertEqual(1, self._count("claims"))
        self.assertEqual(
            1,
            self.kernel.connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key = ?",
                (proposal["idempotency_key"],),
            ).fetchone()[0],
        )

    def test_fr_012_twenty_four_concurrent_evidence_attaches_are_all_preserved(
        self,
    ) -> None:
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.objects["assert_postgresql_proposal"]).outcome,
        )
        ledger_before = self._count("ledger")
        evidence_before = self._count("evidence")
        proposals = [self._attach_proposal(index) for index in range(24)]

        receipts = self._commit_concurrently(proposals)

        self.assertEqual(["COMMITTED"] * 24, sorted(r.outcome for r in receipts))
        self.assertEqual(ledger_before + 24, self._count("ledger"))
        self.assertEqual(evidence_before + 24, self._count("evidence"))
        self.assertEqual(
            25,
            self.kernel.connection.execute(
                "SELECT version FROM claims WHERE claim_id = ?",
                ("claim_atlas_postgresql_001",),
            ).fetchone()[0],
        )
        attached_ids = {
            row["evidence_link_id"]
            for row in self.kernel.connection.execute(
                "SELECT evidence_link_id FROM evidence "
                "WHERE evidence_link_id LIKE 'evidence_concurrent_attach_%'"
            )
        }
        self.assertEqual(
            {f"evidence_concurrent_attach_{index:02d}" for index in range(24)},
            attached_ids,
        )

    def test_fr_023_competing_supersedes_from_one_base_have_one_winner(
        self,
    ) -> None:
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.objects["assert_postgresql_proposal"]).outcome,
        )
        ledger_before = self._count("ledger")
        proposals = [
            self._supersede_proposal(1),
            self._supersede_proposal(2),
        ]

        receipts = self._commit_concurrently(proposals)

        self.assertEqual(
            ["COMMITTED", "TRANSACTION_CONFLICT"],
            sorted(receipt.outcome for receipt in receipts),
        )
        conflict_receipt = next(
            receipt
            for receipt in receipts
            if receipt.outcome == "TRANSACTION_CONFLICT"
        )
        self.assertIn(
            conflict_receipt.reason_codes,
            (("CLAIM_VERSION_MISMATCH",), ("CLAIM_STATUS_MISMATCH",)),
        )
        self.assertEqual(ledger_before + 1, self._count("ledger"))
        target = self.kernel.connection.execute(
            "SELECT status, version, superseded_by FROM claims WHERE claim_id = ?",
            ("claim_atlas_postgresql_001",),
        ).fetchone()
        self.assertEqual("SUPERSEDED", target["status"])
        self.assertEqual(2, target["version"])
        self.assertIn(
            target["superseded_by"],
            {"claim_concurrent_replacement_01", "claim_concurrent_replacement_02"},
        )
        active_replacements = self.kernel.connection.execute(
            "SELECT claim_id FROM claims "
            "WHERE claim_id LIKE 'claim_concurrent_replacement_%' "
            "AND status = 'ACTIVE'"
        ).fetchall()
        self.assertEqual([target["superseded_by"]], [row[0] for row in active_replacements])
        self.assertEqual(
            2,
            self.kernel.connection.execute(
                "SELECT COUNT(*) FROM receipts "
                "WHERE proposal_id LIKE 'proposal_concurrent_supersede_%'"
            ).fetchone()[0],
        )

    def test_fr_014_concurrent_key_reuse_rejects_and_records_losing_attempt(
        self,
    ) -> None:
        ledger_before = self._count("ledger")
        first = self._assertion_with_ids(1)
        second = self._assertion_with_ids(2)
        second["idempotency_key"] = first["idempotency_key"]

        receipts = self._commit_concurrently([first, second])

        self.assertEqual(
            ["COMMITTED", "VALIDATION_ERROR"],
            sorted(receipt.outcome for receipt in receipts),
        )
        rejected = next(
            receipt for receipt in receipts if receipt.outcome == "VALIDATION_ERROR"
        )
        committed = next(receipt for receipt in receipts if receipt.outcome == "COMMITTED")
        self.assertEqual(("IDEMPOTENCY_KEY_REUSE",), rejected.reason_codes)
        self.assertEqual(ledger_before + 1, self._count("ledger"))
        self.assertEqual(1, self._count("claims"))
        self.assertEqual(committed.state_root, rejected.state_root)
        persisted = self.kernel.connection.execute(
            "SELECT proposal_id, outcome, reason_codes, ledger_seq "
            "FROM receipts WHERE idempotency_key = ? ORDER BY proposal_id",
            (first["idempotency_key"],),
        ).fetchall()
        self.assertEqual(2, len(persisted), "every attempt must have a durable receipt")
        self.assertEqual(
            {"COMMITTED", "VALIDATION_ERROR"},
            {row["outcome"] for row in persisted},
        )
        rejected_row = next(
            row for row in persisted if row["outcome"] == "VALIDATION_ERROR"
        )
        self.assertEqual(
            ["IDEMPOTENCY_KEY_REUSE"], json.loads(rejected_row["reason_codes"])
        )
        self.assertIsNone(rejected_row["ledger_seq"])
        self.assertEqual(rejected.proposal_id, rejected_row["proposal_id"])

    def test_nfr_002_rejected_receipt_does_not_advance_head_or_state(self) -> None:
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.objects["assert_postgresql_proposal"]).outcome,
        )
        head_before = self._ledger_head()
        root_before = self.kernel.state_root()
        evidence_before = self._count("evidence")
        proposal = self._attach_proposal(90)
        proposal["operations"][0]["evidence_link"]["selector"]["excerpt_hash"] = (
            "sha256:" + "0" * 64
        )

        receipt = self.kernel.commit(proposal)

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("EVIDENCE_SELECTOR_MISMATCH",), receipt.reason_codes)
        self.assertIsNone(receipt.ledger_seq)
        self.assertEqual(head_before, self._ledger_head())
        self.assertEqual(root_before, self.kernel.state_root())
        self.assertEqual(root_before, receipt.state_root)
        self.assertEqual(evidence_before, self._count("evidence"))
        persisted = self.kernel.connection.execute(
            "SELECT outcome, reason_codes, ledger_seq, state_root "
            "FROM receipts WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        ).fetchall()
        self.assertEqual(1, len(persisted))
        self.assertEqual("VALIDATION_ERROR", persisted[0]["outcome"])
        self.assertEqual(
            ["EVIDENCE_SELECTOR_MISMATCH"],
            json.loads(persisted[0]["reason_codes"]),
        )
        self.assertIsNone(persisted[0]["ledger_seq"])
        self.assertEqual(root_before, persisted[0]["state_root"])

    def test_nfr_003_unexpected_fault_rolls_back_before_it_propagates(self) -> None:
        ledger_before = self._count("ledger")
        root_before = self.kernel.state_root()
        faulty = FaultAfterFirstOperationKernel(self.database, self.registry)
        try:
            with self.assertRaisesRegex(
                InjectedCommitFault, "fault after materialized operation"
            ):
                faulty.commit(self.objects["assert_postgresql_proposal"])

            self.assertFalse(
                faulty.connection.in_transaction,
                "commit must roll back an unexpected fault before re-raising it",
            )
            self.assertEqual(root_before, faulty.state_root())
            self.assertEqual(ledger_before, self._count_on(faulty, "ledger"))
            self.assertEqual(0, self._count_on(faulty, "claims"))
            self.assertEqual(0, self._count_on(faulty, "evidence"))
        finally:
            if faulty.connection.in_transaction:
                faulty.connection.execute("ROLLBACK")
            faulty.close()

        observer = Kernel(self.database, self.registry)
        try:
            self.assertEqual(root_before, observer.state_root())
            self.assertEqual(ledger_before, self._count_on(observer, "ledger"))
            self.assertEqual(0, self._count_on(observer, "claims"))
            self.assertEqual(0, self._count_on(observer, "evidence"))
        finally:
            observer.close()

    def _commit_concurrently(
        self, proposals: list[dict[str, Any]]
    ) -> list[Receipt]:
        barrier = threading.Barrier(len(proposals))

        def commit_one(proposal: dict[str, Any]) -> Receipt:
            kernel = Kernel(self.database, self.registry)
            try:
                # Kernel initialization performs schema and integrity checks on
                # every independent connection.  Under hosted branch coverage,
                # starting 24 such connections can legitimately take longer
                # than ten seconds before the last worker reaches the barrier.
                barrier.wait(timeout=180)
                return kernel.commit(proposal)
            finally:
                kernel.close()

        with ThreadPoolExecutor(max_workers=len(proposals)) as executor:
            futures = [executor.submit(commit_one, proposal) for proposal in proposals]
            # Branch coverage roughly doubles the suite runtime on hosted
            # runners.  Preserve a finite deadlock guard without treating a
            # slow, serialized SQLite writer as a concurrency failure.
            return [future.result(timeout=180) for future in futures]

    def _attach_proposal(self, index: int) -> dict[str, Any]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        link = copy.deepcopy(proposal["operations"][0]["initial_evidence"][0])
        link["evidence_link_id"] = f"evidence_concurrent_attach_{index:02d}"
        proposal["proposal_id"] = f"proposal_concurrent_attach_{index:02d}"
        proposal["idempotency_key"] = f"concurrent-attach-{index:02d}"
        proposal["operations"] = [
            {
                "op_id": f"operation_concurrent_attach_{index:02d}",
                "op": "ATTACH_EVIDENCE",
                "evidence_link": link,
            }
        ]
        return proposal

    def _supersede_proposal(self, index: int) -> dict[str, Any]:
        proposal = copy.deepcopy(self.objects["stale_supersede_proposal"])
        replacement = proposal["operations"][0]["replacement_claim"]
        evidence = proposal["operations"][0]["initial_evidence"][0]
        replacement_id = f"claim_concurrent_replacement_{index:02d}"
        proposal["proposal_id"] = f"proposal_concurrent_supersede_{index:02d}"
        proposal["idempotency_key"] = f"concurrent-supersede-{index:02d}"
        proposal["operations"][0]["op_id"] = (
            f"operation_concurrent_supersede_{index:02d}"
        )
        replacement["claim_id"] = replacement_id
        evidence["claim_id"] = replacement_id
        evidence["evidence_link_id"] = f"evidence_concurrent_replacement_{index:02d}"
        return proposal

    def _assertion_with_ids(self, index: int) -> dict[str, Any]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        operation = proposal["operations"][0]
        claim_id = f"claim_key_reuse_candidate_{index:02d}"
        proposal["proposal_id"] = f"proposal_key_reuse_candidate_{index:02d}"
        proposal["idempotency_key"] = "concurrent-key-reuse-001"
        operation["op_id"] = f"operation_key_reuse_candidate_{index:02d}"
        operation["claim"]["claim_id"] = claim_id
        operation["initial_evidence"][0]["claim_id"] = claim_id
        operation["initial_evidence"][0]["evidence_link_id"] = (
            f"evidence_key_reuse_candidate_{index:02d}"
        )
        return proposal

    def _ledger_head(self) -> tuple[int, str, str] | None:
        row = self.kernel.connection.execute(
            "SELECT seq, entry_hash, state_root FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return None if row is None else (row["seq"], row["entry_hash"], row["state_root"])

    def _count(self, table: str) -> int:
        return self._count_on(self.kernel, table)

    @staticmethod
    def _count_on(kernel: Kernel, table: str) -> int:
        return int(kernel.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
