from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json


ROOT = Path(__file__).resolve().parents[1]


class InjectedReceiptMigrationFault(RuntimeError):
    pass


class ReceiptMigrationAtomicityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_physical_legacy_receipt_migration_rolls_back_every_boundary(
        self,
    ) -> None:
        stages = (
            "receipts_renamed",
            "receipts_created",
            "receipts_copied",
            "receipts_legacy_dropped",
            "legacy_receipts_pinned",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                database = Path(self.temp.name) / f"fault-{stage}.sqlite3"
                self._write_rejected_only_legacy_database(database)
                before = self._database_snapshot(database)

                def inject(_kernel: Kernel, checkpoint: str) -> None:
                    if checkpoint == stage:
                        raise InjectedReceiptMigrationFault(stage)

                with mock.patch.object(
                    Kernel,
                    "_receipt_migration_checkpoint",
                    new=inject,
                    create=True,
                ):
                    with self.assertRaises(InjectedReceiptMigrationFault):
                        Kernel(database, copy.deepcopy(self.registry))

                self.assertEqual(before, self._database_snapshot(database))
                reopened = Kernel(database, copy.deepcopy(self.registry))
                self.addCleanup(reopened.close)
                rows = reopened.connection.execute(
                    "SELECT outcome FROM receipts ORDER BY id"
                ).fetchall()
                self.assertEqual(
                    ["VALIDATION_ERROR", "TRANSACTION_CONFLICT"],
                    [row["outcome"] for row in rows],
                )
                verification = reopened.verify_ledger()
                self.assertTrue(verification["valid"], verification["errors"])

    @staticmethod
    def _write_rejected_only_legacy_database(database: Path) -> None:
        empty_root = sha256_json(
            {table: [] for table in ("sources", "claims", "evidence", "conflicts")}
        )
        connection = sqlite3.connect(database, isolation_level=None)
        try:
            connection.executescript(
                """
                CREATE TABLE sources (
                  revision_id TEXT PRIMARY KEY,
                  content_hash TEXT NOT NULL,
                  document TEXT NOT NULL,
                  content BLOB NOT NULL
                );
                CREATE TABLE claims (
                  claim_id TEXT PRIMARY KEY,
                  proposition_hash TEXT NOT NULL,
                  proposition TEXT NOT NULL,
                  document TEXT NOT NULL,
                  status TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  superseded_by TEXT
                );
                CREATE TABLE evidence (
                  evidence_link_id TEXT PRIMARY KEY,
                  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                  source_revision_id TEXT NOT NULL REFERENCES sources(revision_id),
                  document TEXT NOT NULL
                );
                CREATE TABLE conflicts (
                  conflict_id TEXT PRIMARY KEY,
                  family_key TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  member_digest TEXT NOT NULL,
                  members TEXT NOT NULL,
                  status TEXT NOT NULL,
                  episode INTEGER NOT NULL
                );
                CREATE TABLE ledger (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  prev_hash TEXT,
                  entry_hash TEXT NOT NULL UNIQUE,
                  proposal_hash TEXT NOT NULL,
                  proposal TEXT NOT NULL,
                  events TEXT NOT NULL,
                  state_root TEXT NOT NULL
                );
                CREATE TABLE receipts (
                  idempotency_key TEXT PRIMARY KEY,
                  proposal_hash TEXT NOT NULL,
                  proposal_id TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  reason_codes TEXT NOT NULL,
                  ledger_seq INTEGER,
                  state_root TEXT NOT NULL,
                  conflict_ids TEXT NOT NULL
                );
                """
            )
            rows = (
                (
                    "legacy-validation-rejected-001",
                    "sha256:" + "7" * 64,
                    "proposal_legacy_validation_rejected_001",
                    "VALIDATION_ERROR",
                    canonical_json(["SOURCE_REVISION_NOT_FOUND"]),
                    None,
                    empty_root,
                    "[]",
                ),
                (
                    "legacy-conflict-rejected-001",
                    "sha256:" + "8" * 64,
                    "proposal_legacy_conflict_rejected_001",
                    "TRANSACTION_CONFLICT",
                    canonical_json(["BASE_STATE_ROOT_MISMATCH"]),
                    None,
                    empty_root,
                    "[]",
                ),
            )
            connection.executemany(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
        finally:
            connection.close()

    @staticmethod
    def _database_snapshot(database: Path) -> dict[str, object]:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            objects = [
                tuple(row)
                for row in connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            ]
            tables = {
                row["name"]: [
                    tuple(item)
                    for item in connection.execute(
                        f'SELECT * FROM "{row["name"]}" ORDER BY rowid'
                    )
                ]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            }
            return {"objects": objects, "tables": tables}
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
