from __future__ import annotations

import base64
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared_mind import Kernel


ROOT = Path(__file__).resolve().parents[1]


class AppendOnlyDatabaseConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text()
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text()
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        self.content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", registry)
        receipt = self.kernel.commit(self._source_proposal())
        self.assertEqual("COMMITTED", receipt.outcome)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_nfr_002_ledger_update_is_rejected_by_database(self) -> None:
        before = self._row("SELECT * FROM ledger WHERE seq = 1")

        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.connection.execute(
                "UPDATE ledger SET events = ? WHERE seq = 1",
                ('[{"event_type":"FORGED"}]',),
            )

        self.assertEqual(before, self._row("SELECT * FROM ledger WHERE seq = 1"))

    def test_nfr_002_ledger_delete_is_rejected_by_database(self) -> None:
        before = self._row("SELECT * FROM ledger WHERE seq = 1")

        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.connection.execute("DELETE FROM ledger WHERE seq = 1")

        self.assertEqual(before, self._row("SELECT * FROM ledger WHERE seq = 1"))

    def test_nfr_002_accepted_receipt_update_is_rejected_by_database(self) -> None:
        receipt_id = self._accepted_receipt_id()
        before = self._row("SELECT * FROM receipts WHERE id = ?", (receipt_id,))

        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.connection.execute(
                "UPDATE receipts SET outcome = ? WHERE id = ?",
                ("VALIDATION_ERROR", receipt_id),
            )

        self.assertEqual(
            before, self._row("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
        )

    def test_nfr_002_accepted_receipt_delete_is_rejected_by_database(self) -> None:
        receipt_id = self._accepted_receipt_id()
        before = self._row("SELECT * FROM receipts WHERE id = ?", (receipt_id,))

        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.connection.execute(
                "DELETE FROM receipts WHERE id = ?", (receipt_id,)
            )

        self.assertEqual(
            before, self._row("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
        )

    def test_nfr_004_source_revision_update_is_rejected_by_database(self) -> None:
        revision_id = self.objects["source_revision_postgresql"]["revision_id"]
        before = self._source_row(revision_id)

        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.connection.execute(
                "UPDATE sources SET content_hash = ?, content = ? WHERE revision_id = ?",
                ("sha256:" + "0" * 64, b"forged source bytes", revision_id),
            )

        self.assertEqual(before, self._source_row(revision_id))

    def test_nfr_004_source_revision_replace_is_rejected_by_database(self) -> None:
        revision_id = self.objects["source_revision_postgresql"]["revision_id"]
        before = self._source_row(revision_id)

        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.connection.execute(
                "INSERT OR REPLACE INTO sources "
                "(revision_id, content_hash, document, content) VALUES (?, ?, ?, ?)",
                (
                    revision_id,
                    "sha256:" + "0" * 64,
                    '{"forged":true}',
                    b"forged replacement bytes",
                ),
            )

        self.assertEqual(before, self._source_row(revision_id))

    def _source_proposal(self) -> dict[str, Any]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["proposal_id"] = "proposal_append_only_source_001"
        proposal["idempotency_key"] = "append-only-source-001"
        source = copy.deepcopy(self.objects["source_revision_postgresql"])
        source["blob_ref"] = (
            f"data:{source['media_type']};base64,"
            + base64.b64encode(self.content).decode("ascii")
        )
        proposal["operations"] = [
            {
                "op_id": "operation_append_only_source_001",
                "op": "REGISTER_SOURCE_REVISION",
                "source_revision": source,
            }
        ]
        return proposal

    def _accepted_receipt_id(self) -> int:
        row = self.kernel.connection.execute(
            "SELECT id FROM receipts WHERE ledger_seq IS NOT NULL"
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row["id"])

    def _source_row(self, revision_id: str) -> dict[str, Any]:
        return self._row(
            "SELECT * FROM sources WHERE revision_id = ?", (revision_id,)
        )

    def _row(self, query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any]:
        row = self.kernel.connection.execute(query, parameters).fetchone()
        self.assertIsNotNone(row)
        return dict(row)


if __name__ == "__main__":
    unittest.main()
