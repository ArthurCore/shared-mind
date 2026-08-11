from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json
from shared_mind.validation import build_definition_validator


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "shared-mind-kernel.schema.v1.json"
LEGACY_RECEIPT_SCHEMA_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "receipt_versions"
    / "decision-receipt-1.2.schema.json"
)


class DecisionReceiptSchema13ConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _load_json(CONTRACT_PATH)
        cls.registry = _load_json(
            ROOT / "contracts" / "atlas-predicate-registry.v1.json"
        )
        fixtures = _load_json(
            ROOT / "contracts" / "atlas-conformance-fixtures.v1.json"
        )
        cls.objects = {
            item["name"]: item["object"] for item in fixtures["typed_objects"]
        }
        cls.content = (
            ROOT / "contracts" / "atlas-runbook.fixture.md"
        ).read_bytes()
        legacy_schema = _load_json(LEGACY_RECEIPT_SCHEMA_PATH)
        Draft202012Validator.check_schema(legacy_schema)
        cls.legacy_receipt_validator = Draft202012Validator(
            legacy_schema, format_checker=FormatChecker()
        )
        cls.current_receipt_validator = build_definition_validator(
            "DecisionReceipt"
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_receipt_contract_break_advances_schema_to_1_3(self) -> None:
        self.assertEqual("1.3.0", Kernel.SUPPORTED_VERSIONS["schema"])
        self.assertEqual(
            {"1.0.0", "1.1.0", "1.2.0", "1.3.0"},
            set(Kernel.READABLE_SCHEMA_VERSIONS),
        )
        self.assertIn("DecisionReceiptV1_2", self.contract["$defs"])
        self.assertIn(
            "proposer",
            self.contract["$defs"]["DecisionReceipt"]["required"],
        )
        version_condition = self.contract["$defs"]["VersionBundle"]["allOf"][0]
        self.assertIn(
            "1.3.0",
            version_condition["if"]["properties"]["schema"]["enum"],
        )

        kernel = self._kernel("new-contract.sqlite3")
        receipt = kernel.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]),
            self.content,
        )
        document = receipt.to_contract_dict()
        self.assertEqual([], _messages(self.current_receipt_validator, document))
        self.assertNotEqual([], _messages(self.legacy_receipt_validator, document))

    def test_prior_1_2_database_reopens_without_rewriting_receipt(self) -> None:
        database, expected_document = self._write_prior_1_2_database()
        before_bytes = canonical_json(expected_document)

        reopened = self._kernel("prior-1.2.sqlite3", existing=database)
        row = reopened.connection.execute(
            "SELECT document, proposer, schema_version FROM receipts WHERE id = 1"
        ).fetchone()

        self.assertEqual(before_bytes, row["document"])
        self.assertIsNone(row["proposer"])
        self.assertEqual("1.2.0", row["schema_version"])
        self.assertEqual([], _messages(self.legacy_receipt_validator, expected_document))
        self.assertTrue(reopened.verify_ledger()["valid"])
        self.assertEqual(expected_document, reopened.decision_receipts()[0])
        self.assertNotIn("proposer", reopened.decision_receipts()[0])

    def test_mixed_1_2_to_1_3_history_replays_exact_receipt_versions(self) -> None:
        database, old_document = self._write_prior_1_2_database()
        kernel = self._kernel("mixed.sqlite3", existing=database)
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["versions"]["schema"] = "1.3.0"
        proposal["base_state_root"] = kernel.state_root()

        receipt = kernel.commit(proposal)

        self.assertEqual("COMMITTED", receipt.outcome, receipt.reason_codes)
        rows = kernel.connection.execute(
            "SELECT ledger_seq, document, schema_version FROM receipts ORDER BY id"
        ).fetchall()
        self.assertEqual(["1.2.0", "1.3.0"], [row["schema_version"] for row in rows])
        self.assertEqual(old_document, json.loads(rows[0]["document"]))
        self.assertNotIn("proposer", json.loads(rows[0]["document"]))
        self.assertEqual(proposal["proposer"], json.loads(rows[1]["document"])["proposer"])
        self.assertTrue(kernel.verify_ledger()["valid"], kernel.verify_ledger()["errors"])

        replayed = kernel.replay(Path(self.temp.name) / "mixed-replay.sqlite3")
        self.addCleanup(replayed.close)
        replay_rows = replayed.connection.execute(
            "SELECT document, schema_version FROM receipts ORDER BY id"
        ).fetchall()
        self.assertEqual(["1.2.0", "1.3.0"], [row["schema_version"] for row in replay_rows])
        self.assertEqual(kernel.state_root(), replayed.state_root())
        self.assertEqual(kernel.decision_receipts(), replayed.decision_receipts())
        self.assertTrue(replayed.verify_ledger()["valid"])

    def test_new_1_3_receipts_require_nullable_proposer_field(self) -> None:
        kernel = self._kernel("required-proposer.sqlite3")
        source = copy.deepcopy(self.objects["source_revision_postgresql"])
        receipt = kernel.register_source(source, self.content)
        row = kernel.connection.execute(
            "SELECT document, proposer, schema_version FROM receipts WHERE id = 1"
        ).fetchone()
        document = json.loads(row["document"])

        self.assertEqual("1.3.0", row["schema_version"])
        self.assertEqual(source["registered_by"], document["proposer"])
        self.assertEqual(source["registered_by"], json.loads(row["proposer"]))
        without_proposer = copy.deepcopy(document)
        without_proposer.pop("proposer")
        self.assertNotEqual(
            [], _messages(self.current_receipt_validator, without_proposer)
        )

        malformed = kernel.commit({"unsupported": {"not-json"}})
        malformed_document = malformed.to_contract_dict()
        self.assertIn("proposer", malformed_document)
        self.assertIsNone(malformed_document["proposer"])
        self.assertEqual([], _messages(self.current_receipt_validator, malformed_document))

    def test_verifier_rejects_coordinated_proposer_column_and_document_forgery(
        self,
    ) -> None:
        kernel = self._kernel("forged-proposer.sqlite3")
        kernel.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]), self.content
        )
        row = kernel.connection.execute(
            "SELECT id, document FROM receipts WHERE ledger_seq = 1"
        ).fetchone()
        document = json.loads(row["document"])
        forged = {
            "actor_id": "agent:forged-but-schema-valid",
            "actor_type": "AGENT",
        }
        document["proposer"] = forged
        with kernel._authorized_writes():
            kernel.connection.execute("DROP TRIGGER receipts_no_update")
            kernel.connection.execute(
                "UPDATE receipts SET proposer = ?, document = ? WHERE id = ?",
                (canonical_json(forged), canonical_json(document), row["id"]),
            )

        result = kernel.verify_ledger()

        self.assertFalse(result["valid"])
        self.assertIn(f"RECEIPT_DOCUMENT_MISMATCH:{row['id']}", result["errors"])

    def test_accepted_receipt_fields_are_anchored_to_linked_ledger(self) -> None:
        conflict_id = "conflict_forged_receipt_0001"
        cases = {
            "proposal_id": (
                {"proposal_id": "proposal_forged_receipt_0001"},
                {"proposal_id": "proposal_forged_receipt_0001"},
            ),
            "proposal_hash": (
                {"proposal_hash": "sha256:" + "9" * 64},
                {"proposal_hash": "sha256:" + "9" * 64},
            ),
            "idempotency_key": (
                {"idempotency_key": "fixture-key-repeated"},
                {"idempotency_key": "fixture-key-repeated"},
            ),
            "state_root": (
                {"state_root": "sha256:" + "8" * 64},
                {},
            ),
            "conflict_ids": (
                {"conflict_ids": canonical_json([conflict_id])},
                {"conflict_ids": [conflict_id]},
            ),
            "outcome": (
                {
                    "outcome": "FACT_CONFLICT",
                    "conflict_ids": canonical_json([conflict_id]),
                },
                {"outcome": "FACT_CONFLICT", "conflict_ids": [conflict_id]},
            ),
            "reason_codes": (
                {"reason_codes": canonical_json(["FORGED_ACCEPT_REASON"])},
                {"reason_codes": ["FORGED_ACCEPT_REASON"]},
            ),
            "decided_at": (
                {},
                {"decided_at": "2030-01-01T00:00:00Z"},
            ),
            "ledger_seq": (
                {
                    "ledger_seq": None,
                    "outcome": "VALIDATION_ERROR",
                    "reason_codes": canonical_json(["FORGED_REJECTION"]),
                },
                {
                    "outcome": "VALIDATION_ERROR",
                    "reason_codes": ["FORGED_REJECTION"],
                    "head_before": None,
                    "head_after": None,
                    "ledger_entry_id": None,
                },
            ),
        }
        for name, (row_updates, document_updates) in cases.items():
            with self.subTest(field=name):
                kernel = self._kernel(f"forged-{name}.sqlite3")
                kernel.register_source(
                    copy.deepcopy(self.objects["source_revision_postgresql"]),
                    self.content,
                )
                row = kernel.connection.execute(
                    "SELECT * FROM receipts WHERE id = 1"
                ).fetchone()
                document = json.loads(row["document"])
                document.update(document_updates)
                assignments = list(row_updates) + ["document"]
                values = list(row_updates.values()) + [canonical_json(document)]
                with kernel._authorized_writes():
                    kernel.connection.execute("DROP TRIGGER receipts_no_update")
                    kernel.connection.execute(
                        "UPDATE receipts SET "
                        + ", ".join(f"{field} = ?" for field in assignments)
                        + " WHERE id = ?",
                        (*values, row["id"]),
                    )

                result = kernel.verify_ledger()

                self.assertFalse(result["valid"], (name, result))

    def test_accepted_receipt_coverage_is_exactly_one_per_ledger_entry(self) -> None:
        missing = self._kernel("missing-accepted-receipt.sqlite3")
        missing.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]), self.content
        )
        with missing._authorized_writes():
            missing.connection.execute("DROP TRIGGER receipts_no_delete")
            missing.connection.execute("DELETE FROM receipts WHERE ledger_seq = 1")

        missing_result = missing.verify_ledger()

        self.assertFalse(missing_result["valid"])
        self.assertIn(
            "ACCEPTED_RECEIPT_COVERAGE_MISMATCH:1:0",
            missing_result["errors"],
        )

        duplicate = self._kernel("duplicate-accepted-receipt.sqlite3")
        duplicate.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]), self.content
        )
        row = duplicate.connection.execute(
            "SELECT * FROM receipts WHERE ledger_seq = 1"
        ).fetchone()
        document = json.loads(row["document"])
        document["receipt_id"] = "receipt_decision_00000000000000000002"
        with duplicate._authorized_writes():
            duplicate.connection.execute(
                """INSERT INTO receipts(
                     idempotency_key, proposal_hash, proposal_id, outcome,
                     reason_codes, ledger_seq, state_root, conflict_ids, proposer,
                     document, schema_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["idempotency_key"],
                    row["proposal_hash"],
                    row["proposal_id"],
                    row["outcome"],
                    row["reason_codes"],
                    row["ledger_seq"],
                    row["state_root"],
                    row["conflict_ids"],
                    row["proposer"],
                    canonical_json(document),
                    row["schema_version"],
                ),
            )

        duplicate_result = duplicate.verify_ledger()

        self.assertFalse(duplicate_result["valid"])
        self.assertIn(
            "ACCEPTED_RECEIPT_COVERAGE_MISMATCH:1:2",
            duplicate_result["errors"],
        )

    def test_transitional_1_2_proposer_receipt_reopens_and_replays_exactly(
        self,
    ) -> None:
        database, expected_document = self._write_prior_1_2_database(
            include_proposer=True
        )
        expected_bytes = canonical_json(expected_document)
        expected_proposer = expected_document["proposer"]
        reopened = self._kernel("transitional-1.2.sqlite3", existing=database)
        row = reopened.connection.execute(
            "SELECT document, proposer, schema_version FROM receipts WHERE id = 1"
        ).fetchone()

        self.assertEqual(expected_bytes, row["document"])
        self.assertEqual(canonical_json(expected_proposer), row["proposer"])
        self.assertEqual("1.2.0", row["schema_version"])
        self.assertTrue(reopened.verify_ledger()["valid"])

        replayed = reopened.replay(
            Path(self.temp.name) / "transitional-1.2-replay.sqlite3"
        )
        self.addCleanup(replayed.close)
        replay_row = replayed.connection.execute(
            "SELECT document, proposer, schema_version FROM receipts WHERE id = 1"
        ).fetchone()
        self.assertEqual(expected_bytes, replay_row["document"])
        self.assertEqual(canonical_json(expected_proposer), replay_row["proposer"])
        self.assertEqual("1.2.0", replay_row["schema_version"])
        self.assertEqual(reopened.decision_receipts(), replayed.decision_receipts())
        self.assertTrue(replayed.verify_ledger()["valid"])

    def test_accepted_receipt_coverage_uses_one_batch_query(self) -> None:
        kernel = self._kernel("coverage-query-shape.sqlite3")
        self._populate_three_ledger_entries(kernel)
        statements: list[str] = []
        connection = kernel.connection._PublicConnection__connection
        connection.set_trace_callback(statements.append)

        errors = kernel._verify_accepted_receipt_coverage(
            kernel.connection.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
        )
        connection.set_trace_callback(None)

        receipt_reads = [
            " ".join(statement.lower().split())
            for statement in statements
            if statement.lstrip().lower().startswith("select")
            and "from receipts" in statement.lower()
        ]
        self.assertEqual([], errors)
        self.assertEqual(1, len(receipt_reads), receipt_reads)
        self.assertIn("group by ledger_seq", receipt_reads[0])

    def test_replay_batches_accepted_receipt_documents(self) -> None:
        kernel = self._kernel("replay-query-shape.sqlite3")
        self._populate_three_ledger_entries(kernel)
        statements: list[str] = []
        connection = kernel.connection._PublicConnection__connection
        connection.set_trace_callback(statements.append)

        replayed = kernel.replay(Path(self.temp.name) / "replay-query-shape-copy.sqlite3")
        self.addCleanup(replayed.close)
        connection.set_trace_callback(None)

        receipt_reads = [
            " ".join(statement.lower().split())
            for statement in statements
            if statement.lstrip().lower().startswith("select")
            and "from receipts" in statement.lower()
        ]
        per_entry_document_reads = [
            statement
            for statement in receipt_reads
            if "select document from receipts where ledger_seq =" in statement
        ]
        batch_document_reads = [
            statement
            for statement in receipt_reads
            if "select * from receipts order by id" in statement
        ]
        self.assertEqual([], per_entry_document_reads, per_entry_document_reads)
        self.assertEqual(1, len(batch_document_reads), receipt_reads)

    def test_replay_preserves_interleaved_current_receipt_audit_stream(
        self,
    ) -> None:
        kernel = self._kernel("interleaved-receipts.sqlite3")
        kernel.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]), self.content
        )
        rejected_proposal = copy.deepcopy(
            self.objects["assert_postgresql_proposal"]
        )
        rejected_proposal["base_state_root"] = kernel.state_root()
        rejected_proposal["operations"][0]["initial_evidence"][0]["selector"][
            "excerpt_hash"
        ] = "sha256:" + "0" * 64
        self.assertEqual("VALIDATION_ERROR", kernel.commit(rejected_proposal).outcome)
        accepted_proposal = copy.deepcopy(
            self.objects["assert_postgresql_proposal"]
        )
        accepted_proposal["proposal_id"] = "proposal_replay_audit_accepted_001"
        accepted_proposal["idempotency_key"] = "replay-audit-accepted-001"
        accepted_proposal["base_state_root"] = kernel.state_root()
        self.assertEqual("COMMITTED", kernel.commit(accepted_proposal).outcome)
        source_rows = [
            dict(row)
            for row in kernel.connection.execute(
                "SELECT * FROM receipts ORDER BY id"
            )
        ]

        replayed = kernel.replay(
            Path(self.temp.name) / "interleaved-receipts-replay.sqlite3"
        )
        self.addCleanup(replayed.close)
        replay_rows = [
            dict(row)
            for row in replayed.connection.execute(
                "SELECT * FROM receipts ORDER BY id"
            )
        ]

        self.assertEqual(source_rows, replay_rows)
        self.assertTrue(replayed.verify_ledger()["valid"])

    def test_rejected_1_3_receipt_preserves_actor_without_advancing_head(self) -> None:
        kernel = self._kernel("rejected-current.sqlite3")
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["versions"]["schema"] = "1.3.0"
        proposal["base_state_root"] = kernel.state_root()
        head_before = kernel._head_entry_hash()

        receipt = kernel.commit(proposal)
        row = kernel.connection.execute(
            "SELECT * FROM receipts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        document = json.loads(row["document"])

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertIsNone(receipt.ledger_seq)
        self.assertEqual(head_before, kernel._head_entry_hash())
        self.assertEqual("1.3.0", row["schema_version"])
        self.assertEqual(proposal["proposer"], json.loads(row["proposer"]))
        self.assertEqual(proposal["proposer"], document["proposer"])
        self.assertEqual(document["head_before"], document["head_after"])
        self.assertEqual([], _messages(self.current_receipt_validator, document))
        self.assertTrue(kernel.verify_ledger()["valid"])

    def test_rejected_receipt_state_root_is_anchored_to_historical_head(
        self,
    ) -> None:
        for has_ledger_head in (False, True):
            with self.subTest(has_ledger_head=has_ledger_head):
                kernel = self._kernel(
                    f"rejected-root-{has_ledger_head}.sqlite3"
                )
                if has_ledger_head:
                    kernel.register_source(
                        copy.deepcopy(
                            self.objects["source_revision_postgresql"]
                        ),
                        self.content,
                    )
                proposal = copy.deepcopy(
                    self.objects["assert_postgresql_proposal"]
                )
                proposal["base_state_root"] = kernel.state_root()
                if has_ledger_head:
                    proposal["operations"][0]["initial_evidence"][0]["selector"][
                        "excerpt_hash"
                    ] = "sha256:" + "0" * 64
                receipt = kernel.commit(proposal)
                self.assertEqual("VALIDATION_ERROR", receipt.outcome)
                row = kernel.connection.execute(
                    "SELECT id, state_root, document FROM receipts "
                    "WHERE ledger_seq IS NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
                original_document = row["document"]
                self.assertEqual(kernel.state_root(), row["state_root"])
                with kernel._authorized_writes():
                    kernel.connection.execute("DROP TRIGGER receipts_no_update")
                    kernel.connection.execute(
                        "UPDATE receipts SET state_root = ? WHERE id = ?",
                        ("sha256:" + "7" * 64, row["id"]),
                    )

                result = kernel.verify_ledger()

                self.assertFalse(result["valid"])
                self.assertIn(
                    f"RECEIPT_DOCUMENT_MISMATCH:{row['id']}",
                    result["errors"],
                )
                self.assertEqual(
                    original_document,
                    kernel.connection.execute(
                        "SELECT document FROM receipts WHERE id = ?", (row["id"],)
                    ).fetchone()["document"],
                )

    def _kernel(self, name: str, *, existing: Path | None = None) -> Kernel:
        database = existing if existing is not None else Path(self.temp.name) / name
        kernel = Kernel(database, copy.deepcopy(self.registry))
        self.addCleanup(kernel.close)
        return kernel

    def _populate_three_ledger_entries(self, kernel: Kernel) -> None:
        kernel.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]), self.content
        )
        for name in (
            "assert_postgresql_proposal",
            "assert_mysql_same_interval_proposal",
        ):
            proposal = copy.deepcopy(self.objects[name])
            proposal["base_state_root"] = kernel.state_root()
            receipt = kernel.commit(proposal)
            self.assertIn(receipt.outcome, {"COMMITTED", "FACT_CONFLICT"})

    def _write_prior_1_2_database(
        self, *, include_proposer: bool = False
    ) -> tuple[Path, dict[str, Any]]:
        database = Path(self.temp.name) / "prior-1.2.sqlite3"
        kernel = Kernel(database, copy.deepcopy(self.registry))
        kernel.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]), self.content
        )
        ledger = kernel.connection.execute(
            "SELECT * FROM ledger WHERE seq = 1"
        ).fetchone()
        proposal = json.loads(ledger["proposal"])
        proposal["versions"]["schema"] = "1.2.0"
        events = json.loads(ledger["events"])
        proposal_hash = sha256_json(proposal)
        entry_hash = sha256_json(
            Kernel._ledger_envelope(
                seq=1,
                prev_hash=None,
                proposal_hash=proposal_hash,
                pre_state_root=ledger["pre_state_root"],
                post_state_root=ledger["state_root"],
                versions=proposal["versions"],
                events=events,
                committed_at=ledger["committed_at"],
            )
        )
        ledger_document = json.loads(ledger["document"])
        ledger_document.update(
            {
                "entry_hash": entry_hash,
                "proposal_hash": proposal_hash,
                "versions": proposal["versions"],
            }
        )
        receipt = kernel.connection.execute(
            "SELECT * FROM receipts WHERE id = 1"
        ).fetchone()
        receipt_document = json.loads(receipt["document"])
        stored_proposer = receipt_document.get("proposer")
        if not include_proposer:
            receipt_document.pop("proposer", None)
            stored_proposer = None
        receipt_document["proposal_hash"] = proposal_hash
        receipt_document["head_after"] = entry_hash

        with kernel._authorized_writes():
            kernel.connection.execute("DROP TRIGGER ledger_no_update")
            kernel.connection.execute("DROP TRIGGER receipts_no_update")
            kernel.connection.execute(
                """UPDATE ledger SET proposal = ?, proposal_hash = ?,
                   entry_hash = ?, document = ? WHERE seq = 1""",
                (
                    canonical_json(proposal),
                    proposal_hash,
                    entry_hash,
                    canonical_json(ledger_document),
                ),
            )
            kernel.connection.execute(
                """UPDATE receipts SET proposal_hash = ?, proposer = ?,
                   document = ?, schema_version = '1.2.0' WHERE id = 1""",
                (
                    proposal_hash,
                    (
                        None
                        if stored_proposer is None
                        else canonical_json(stored_proposer)
                    ),
                    canonical_json(receipt_document),
                ),
            )
        kernel.close()
        return database, receipt_document


def _messages(
    validator: Draft202012Validator, instance: Any
) -> list[str]:
    return sorted(error.message for error in validator.iter_errors(instance))


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


if __name__ == "__main__":
    unittest.main()
