from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json
from shared_mind.validation import build_definition_validator
from shared_mind.projection import project_json


ROOT = Path(__file__).resolve().parents[1]


class PersistedContractDocumentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        cls.registry = registry
        cls.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        cls.content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        cls.ledger_validator = build_definition_validator("LedgerEntry")
        cls.receipt_validator = build_definition_validator("DecisionReceipt")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kernel = Kernel(
            Path(self.temp.name) / "kernel.sqlite3", copy.deepcopy(self.registry)
        )
        receipt = self.kernel.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]), self.content
        )
        self.assertEqual("COMMITTED", receipt.outcome)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_every_ledger_row_persists_a_schema_valid_canonical_document(self) -> None:
        outcomes = self._exercise_all_outcomes()
        self.assertIn("FACT_CONFLICT", outcomes)
        self._require_columns("ledger", {"document"})

        rows = self.kernel.connection.execute(
            "SELECT * FROM ledger ORDER BY seq"
        ).fetchall()

        self.assertGreaterEqual(len(rows), 4)
        for row in rows:
            with self.subTest(sequence=row["seq"]):
                document = json.loads(row["document"])
                self.assertEqual(
                    [],
                    self._messages(self.ledger_validator, document),
                    "persisted ledger document must satisfy LedgerEntry@1.1",
                )
                self.assertEqual(canonical_json(document), row["document"])
                proposal = json.loads(row["proposal"])
                events = json.loads(row["events"])
                expected = {
                    "seq": row["seq"],
                    "prev_hash": row["prev_hash"],
                    "entry_hash": row["entry_hash"],
                    "pre_state_root": row["pre_state_root"],
                    "post_state_root": row["state_root"],
                    "proposal_id": proposal["proposal_id"],
                    "proposal_hash": row["proposal_hash"],
                    "versions": proposal["versions"],
                    "events": events,
                    "committed_at": row["committed_at"],
                }
                for field, value in expected.items():
                    self.assertEqual(value, document[field], field)
                self.assertEqual("LEDGER_ENTRY", document["object_type"])
                self.assertEqual(
                    row["entry_hash"],
                    sha256_json(
                        Kernel._ledger_envelope(
                            seq=int(row["seq"]),
                            prev_hash=row["prev_hash"],
                            proposal_hash=row["proposal_hash"],
                            pre_state_root=row["pre_state_root"],
                            post_state_root=row["state_root"],
                            versions=proposal["versions"],
                            events=events,
                            committed_at=row["committed_at"],
                        )
                    ),
                )

    def test_all_outcome_receipts_are_schema_valid_and_match_normalized_columns(
        self,
    ) -> None:
        outcomes = self._exercise_all_outcomes()
        self.assertTrue(
            {
                "COMMITTED",
                "FACT_CONFLICT",
                "TRANSACTION_CONFLICT",
                "VALIDATION_ERROR",
            }.issubset(outcomes)
        )
        self._require_columns("ledger", {"document"})
        self._require_columns("receipts", {"document"})

        rows = self.kernel.connection.execute(
            "SELECT * FROM receipts ORDER BY id"
        ).fetchall()
        final_head = self._head_hash()

        for row in rows:
            with self.subTest(receipt_id=row["id"], outcome=row["outcome"]):
                document = json.loads(row["document"])
                self.assertEqual(
                    [],
                    self._messages(self.receipt_validator, document),
                    "persisted receipt document must satisfy DecisionReceipt@1.1",
                )
                self.assertEqual(canonical_json(document), row["document"])
                parity = {
                    "proposal_id": row["proposal_id"],
                    "proposal_hash": row["proposal_hash"],
                    "idempotency_key": row["idempotency_key"],
                    "outcome": row["outcome"],
                    "reason_codes": json.loads(row["reason_codes"]),
                    "conflict_ids": json.loads(row["conflict_ids"]),
                }
                for field, value in parity.items():
                    self.assertEqual(value, document[field], field)
                self.assertEqual("DECISION_RECEIPT", document["object_type"])

                if row["ledger_seq"] is None:
                    self.assertIsNone(document["ledger_entry_id"])
                    self.assertEqual(document["head_before"], document["head_after"])
                    self.assertEqual(final_head, document["head_after"])
                else:
                    ledger = self.kernel.connection.execute(
                        "SELECT * FROM ledger WHERE seq = ?", (row["ledger_seq"],)
                    ).fetchone()
                    self.assertIsNotNone(ledger)
                    ledger_document = json.loads(ledger["document"])
                    self.assertEqual(
                        ledger_document["entry_id"], document["ledger_entry_id"]
                    )
                    self.assertEqual(ledger["prev_hash"], document["head_before"])
                    self.assertEqual(ledger["entry_hash"], document["head_after"])

    def test_decision_receipt_contract_can_represent_a_non_json_attempt(self) -> None:
        candidate = {
            "object_type": "DECISION_RECEIPT",
            "receipt_id": "receipt_malformed_0001",
            "proposal_id": None,
            "proposal_hash": "sha256:" + "1" * 64,
            "idempotency_key": None,
            "outcome": "VALIDATION_ERROR",
            "reason_codes": ["MALFORMED_PROPOSAL"],
            "head_before": None,
            "head_after": None,
            "ledger_entry_id": None,
            "conflict_ids": [],
            "decided_at": "2026-08-11T00:00:00Z",
        }

        self.assertEqual(
            [],
            self._messages(self.receipt_validator, candidate),
            "DecisionReceipt@1.1 must allow explicit null identifiers when the "
            "submitted value is not a JSON Proposal",
        )

    def test_non_json_attempt_persists_a_schema_valid_receipt_document(self) -> None:
        receipt = self.kernel.commit({"unsupported": {"set"}})
        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("MALFORMED_PROPOSAL",), receipt.reason_codes)
        self._require_columns("receipts", {"document"})

        row = self.kernel.connection.execute(
            "SELECT * FROM receipts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        document = json.loads(row["document"])

        self.assertEqual([], self._messages(self.receipt_validator, document))
        self.assertEqual(document, receipt.to_contract_dict())
        self.assertIsNone(document["proposal_id"])
        self.assertIsNone(document["idempotency_key"])
        self.assertEqual("VALIDATION_ERROR", document["outcome"])
        self.assertEqual(["MALFORMED_PROPOSAL"], document["reason_codes"])
        self.assertIsNone(document["ledger_entry_id"])
        self.assertEqual(document["head_before"], document["head_after"])
        self.assertEqual(self._head_hash(), document["head_after"])

    def test_projection_preserves_each_exact_ledger_contract_document(self) -> None:
        self._exercise_all_outcomes()
        projection = json.loads(project_json(self.kernel))
        persisted = {
            int(row["seq"]): json.loads(row["document"])
            for row in self.kernel.connection.execute(
                "SELECT seq, document FROM ledger ORDER BY seq"
            )
        }

        for item in projection["ledger"]["entries"]:
            with self.subTest(sequence=item["sequence"]):
                self.assertEqual(
                    persisted[item["sequence"]], item["ledger_entry"]
                )
                self.assertEqual(
                    [],
                    self._messages(
                        self.ledger_validator, item["ledger_entry"]
                    ),
                )

    def test_verifier_rejects_receipt_document_column_drift(self) -> None:
        row = self.kernel.connection.execute(
            "SELECT id, document FROM receipts ORDER BY id LIMIT 1"
        ).fetchone()
        document = json.loads(row["document"])
        document["proposal_hash"] = "sha256:" + "0" * 64
        with self.kernel._authorized_writes():
            self.kernel.connection.execute("DROP TRIGGER receipts_no_update")
            self.kernel.connection.execute(
                "UPDATE receipts SET document = ? WHERE id = ?",
                (canonical_json(document), row["id"]),
            )

        result = self.kernel.verify_ledger()

        self.assertFalse(result["valid"])
        self.assertIn(
            f"RECEIPT_DOCUMENT_MISMATCH:{row['id']}", result["errors"]
        )

    def test_verifier_rejects_missing_documents_for_current_receipts(self) -> None:
        rejected = self.kernel.commit(self._invalid_evidence_proposal())
        self.assertEqual("VALIDATION_ERROR", rejected.outcome)
        rows = self.kernel.connection.execute(
            "SELECT id FROM receipts ORDER BY id"
        ).fetchall()
        self.assertEqual(2, len(rows))
        with self.kernel._authorized_writes():
            self.kernel.connection.execute("DROP TRIGGER receipts_no_update")
            self.kernel.connection.execute("UPDATE receipts SET document = NULL")

        result = self.kernel.verify_ledger()

        self.assertFalse(result["valid"])
        for row in rows:
            self.assertIn(
                f"RECEIPT_DOCUMENT_MISMATCH:{row['id']}", result["errors"]
            )

    def _exercise_all_outcomes(self) -> set[str]:
        receipts = []
        receipts.append(
            self.kernel.commit(copy.deepcopy(self.objects["assert_postgresql_proposal"]))
        )
        receipts.append(
            self.kernel.commit(
                copy.deepcopy(self.objects["assert_mysql_same_interval_proposal"])
            )
        )
        receipts.append(self.kernel.commit(self._attach_evidence_proposal()))
        receipts.append(
            self.kernel.commit(copy.deepcopy(self.objects["stale_supersede_proposal"]))
        )
        receipts.append(self.kernel.commit(self._invalid_evidence_proposal()))
        return {receipt.outcome for receipt in receipts} | {"COMMITTED"}

    def _attach_evidence_proposal(self) -> dict[str, Any]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        link = copy.deepcopy(proposal["operations"][0]["initial_evidence"][0])
        proposal["proposal_id"] = "proposal_contract_attach_001"
        proposal["idempotency_key"] = "contract-attach-001"
        link["evidence_link_id"] = "evidence_contract_attach_001"
        proposal["operations"] = [
            {
                "op_id": "operation_contract_attach_001",
                "op": "ATTACH_EVIDENCE",
                "evidence_link": link,
            }
        ]
        return proposal

    def _invalid_evidence_proposal(self) -> dict[str, Any]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        operation = proposal["operations"][0]
        claim_id = "claim_contract_invalid_001"
        proposal["proposal_id"] = "proposal_contract_invalid_001"
        proposal["idempotency_key"] = "contract-invalid-001"
        operation["op_id"] = "operation_contract_invalid_001"
        operation["claim"]["claim_id"] = claim_id
        operation["initial_evidence"][0]["claim_id"] = claim_id
        operation["initial_evidence"][0]["evidence_link_id"] = (
            "evidence_contract_invalid_001"
        )
        operation["initial_evidence"][0]["selector"]["excerpt_hash"] = (
            "sha256:" + "0" * 64
        )
        return proposal

    def _head_hash(self) -> str | None:
        row = self.kernel.connection.execute(
            "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row["entry_hash"])

    def _require_columns(self, table: str, expected: set[str]) -> None:
        cursor = self.kernel.connection.execute(f'SELECT * FROM "{table}" LIMIT 0')
        actual = {str(column[0]) for column in cursor.description}
        self.assertFalse(
            expected - actual,
            f"{table} must persist canonical contract column(s): "
            + ", ".join(sorted(expected - actual)),
        )

    @staticmethod
    def _messages(validator: Any, document: object) -> list[str]:
        return [
            error.message
            for error in sorted(
                validator.iter_errors(document),
                key=lambda item: (list(item.absolute_path), item.message),
            )
        ]


if __name__ == "__main__":
    unittest.main()
