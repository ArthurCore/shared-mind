from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_bytes, sha256_json
from shared_mind.kernel import ValidationFailure
from shared_mind.projection import project_json


ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "3c3cdf0"
BASELINE_SCHEMA_VERSION = "1.0.0"
BASELINE_STATE_ROOT = (
    "sha256:b41e948fa372b6c77d989b8508e7c39da339d23c596d622b470a20dfa71cccca"
)


class LegacyMigrationConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "baseline-3c3cdf0.sqlite3"
        self.replay_database = Path(self.temp.name) / "replayed-current.sqlite3"
        self.registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        self.legacy_entry = self._write_baseline_database()

    def test_schema_change_advances_the_pinned_contract_version(self) -> None:
        current = tuple(
            int(part) for part in Kernel.SUPPORTED_VERSIONS["schema"].split(".")
        )
        baseline = tuple(int(part) for part in BASELINE_SCHEMA_VERSION.split("."))

        self.assertGreater(
            current,
            baseline,
            "continuity records and the state-root domain changed after "
            f"{BASELINE_COMMIT}; NFR-011 requires a new schema version",
        )

    def test_baseline_database_reopens_verifies_and_replays_without_rewriting_history(
        self,
    ) -> None:
        kernel = Kernel(self.database, self.registry)
        self.addCleanup(kernel.close)

        verification = kernel.verify_ledger()
        historical = kernel.connection.execute(
            """
            SELECT prev_hash, entry_hash, proposal_hash, proposal, events, state_root
            FROM ledger WHERE seq = 1
            """
        ).fetchone()

        self.assertEqual(self.legacy_entry, dict(historical))
        self.assertTrue(
            verification["valid"],
            "the current verifier must dispatch the baseline ledger format and "
            f"schema {BASELINE_SCHEMA_VERSION}: {verification['errors']}",
        )
        projection = json.loads(project_json(kernel))
        self.assertEqual("markdown-projection@3", projection["projection_version"])
        self.assertEqual(kernel.state_root(), projection["state_root"])
        replayed = kernel.replay(self.replay_database)
        self.addCleanup(replayed.close)
        self.assertEqual(kernel.state_root(), replayed.state_root())
        self.assertTrue(replayed.verify_ledger()["valid"])
        self.assertEqual(
            ["claim_atlas_postgresql_001"],
            [
                row["claim_id"]
                for row in replayed.connection.execute(
                    "SELECT claim_id FROM claims ORDER BY claim_id"
                )
            ],
        )

    def test_current_entry_can_follow_legacy_history_and_replay_the_version_boundary(
        self,
    ) -> None:
        kernel = Kernel(self.database, self.registry)
        self.addCleanup(kernel.close)
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        evidence = copy.deepcopy(proposal["operations"][0]["initial_evidence"][0])
        evidence["evidence_link_id"] = "evidence_after_schema_migration_001"
        proposal["proposal_id"] = "proposal_after_schema_migration_001"
        proposal["idempotency_key"] = "after-schema-migration-001"
        proposal["operations"] = [
            {
                "op_id": "operation_after_schema_migration_001",
                "op": "ATTACH_EVIDENCE",
                "evidence_link": evidence,
            }
        ]

        receipt = kernel.commit(proposal)

        self.assertEqual("COMMITTED", receipt.outcome)
        self.assertEqual(2, receipt.ledger_seq)
        self.assertTrue(kernel.verify_ledger()["valid"])
        replayed = kernel.replay(self.replay_database)
        self.addCleanup(replayed.close)
        self.assertEqual(kernel.state_root(), replayed.state_root())
        self.assertTrue(replayed.verify_ledger()["valid"])

    def test_baseline_rejected_receipts_reopen_and_replay_exactly(self) -> None:
        kernel = Kernel(self.database, self.registry)
        self.addCleanup(kernel.close)

        source_rows = [
            dict(row)
            for row in kernel.connection.execute(
                "SELECT * FROM receipts ORDER BY id"
            )
        ]
        verification = kernel.verify_ledger()

        self.assertTrue(verification["valid"], verification["errors"])
        self.assertEqual(
            ["COMMITTED", "VALIDATION_ERROR", "TRANSACTION_CONFLICT"],
            [row["outcome"] for row in source_rows],
        )
        self.assertEqual(
            [None, None, None],
            [row["schema_version"] for row in source_rows],
        )
        self.assertEqual([None, None, None], [row["document"] for row in source_rows])

        replayed = kernel.replay(self.replay_database)
        self.addCleanup(replayed.close)
        replay_rows = [
            dict(row)
            for row in replayed.connection.execute(
                "SELECT * FROM receipts ORDER BY id"
            )
        ]

        self.assertEqual(source_rows, replay_rows)
        self.assertTrue(replayed.verify_ledger()["valid"])

    def test_staged_legacy_receipt_migration_backfills_provenance_pin(self) -> None:
        initially_migrated = Kernel(self.database, self.registry)
        initially_migrated.close()
        connection = sqlite3.connect(self.database, isolation_level=None)
        try:
            connection.execute("DROP TRIGGER receipts_no_update")
            connection.execute(
                "UPDATE receipts SET schema_version = NULL "
                "WHERE document IS NULL"
            )
            connection.execute(
                "DELETE FROM kernel_metadata WHERE name = ?",
                (Kernel._LEGACY_RECEIPT_PIN_NAME,),
            )
        finally:
            connection.close()

        reopened = Kernel(self.database, self.registry)
        self.addCleanup(reopened.close)
        source_rows = [
            dict(row)
            for row in reopened.connection.execute(
                "SELECT * FROM receipts ORDER BY id"
            )
        ]

        self.assertTrue(reopened.verify_ledger()["valid"])
        self.assertEqual([None, None, None], [row["schema_version"] for row in source_rows])
        self.assertIsNotNone(
            reopened.connection.execute(
                "SELECT value FROM kernel_metadata WHERE name = ?",
                (Kernel._LEGACY_RECEIPT_PIN_NAME,),
            ).fetchone()
        )
        replayed = reopened.replay(
            Path(self.temp.name) / "staged-legacy-replay.sqlite3"
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

    def test_staged_legacy_pin_allows_later_schema_1_2_history_only(self) -> None:
        staged = Kernel(self.database, self.registry)
        evidence = copy.deepcopy(
            self.objects["assert_postgresql_proposal"]["operations"][0][
                "initial_evidence"
            ][0]
        )
        evidence["evidence_link_id"] = "evidence_staged_schema_1_2_001"
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["proposal_id"] = "proposal_staged_schema_1_2_001"
        proposal["idempotency_key"] = "staged-schema-1-2-001"
        proposal["base_state_root"] = staged.state_root()
        proposal["operations"] = [
            {
                "op_id": "operation_staged_schema_1_2_001",
                "op": "ATTACH_EVIDENCE",
                "evidence_link": evidence,
            }
        ]
        self.assertEqual("COMMITTED", staged.commit(proposal).outcome)
        ledger = staged.connection.execute(
            "SELECT * FROM ledger WHERE seq = 2"
        ).fetchone()
        stored_proposal = json.loads(ledger["proposal"])
        stored_proposal["versions"]["schema"] = "1.2.0"
        proposal_hash = sha256_json(stored_proposal)
        events = json.loads(ledger["events"])
        entry_hash = sha256_json(
            Kernel._ledger_envelope(
                seq=2,
                prev_hash=ledger["prev_hash"],
                proposal_hash=proposal_hash,
                pre_state_root=ledger["pre_state_root"],
                post_state_root=ledger["state_root"],
                versions=stored_proposal["versions"],
                events=events,
                committed_at=ledger["committed_at"],
            )
        )
        ledger_document = json.loads(ledger["document"])
        ledger_document.update(
            {
                "entry_hash": entry_hash,
                "proposal_hash": proposal_hash,
                "versions": stored_proposal["versions"],
            }
        )
        receipt = staged.connection.execute(
            "SELECT * FROM receipts WHERE ledger_seq = 2"
        ).fetchone()
        receipt_document = json.loads(receipt["document"])
        receipt_document.pop("proposer")
        receipt_document["proposal_hash"] = proposal_hash
        receipt_document["head_after"] = entry_hash
        with staged._authorized_writes():
            staged.connection.execute("DROP TRIGGER ledger_no_update")
            staged.connection.execute("DROP TRIGGER receipts_no_update")
            staged.connection.execute(
                "UPDATE ledger SET proposal = ?, proposal_hash = ?, "
                "entry_hash = ?, document = ? WHERE seq = 2",
                (
                    canonical_json(stored_proposal),
                    proposal_hash,
                    entry_hash,
                    canonical_json(ledger_document),
                ),
            )
            staged.connection.execute(
                "UPDATE receipts SET proposal_hash = ?, proposer = NULL, "
                "document = ?, schema_version = '1.2.0' WHERE ledger_seq = 2",
                (proposal_hash, canonical_json(receipt_document)),
            )
            staged.connection.execute(
                "DELETE FROM kernel_metadata WHERE name = ?",
                (Kernel._LEGACY_RECEIPT_PIN_NAME,),
            )
            staged.connection._PublicConnection__connection.executescript(
                """
                CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
                BEGIN SELECT RAISE(ABORT, 'LEDGER_APPEND_ONLY'); END;
                CREATE TRIGGER receipts_no_update BEFORE UPDATE ON receipts
                BEGIN SELECT RAISE(ABORT, 'RECEIPT_APPEND_ONLY'); END;
                """
            )
        staged.close()

        reopened = Kernel(self.database, self.registry)
        self.addCleanup(reopened.close)
        source_rows = [
            dict(row)
            for row in reopened.connection.execute(
                "SELECT * FROM receipts ORDER BY id"
            )
        ]

        self.assertTrue(reopened.verify_ledger()["valid"])
        self.assertEqual(
            ["1.0.0", "1.2.0"],
            sorted(
                {
                    json.loads(row["proposal"])["versions"]["schema"]
                    for row in reopened.connection.execute(
                        "SELECT proposal FROM ledger ORDER BY seq"
                    )
                }
            ),
        )
        replayed = reopened.replay(
            Path(self.temp.name) / "mixed-staged-legacy-replay.sqlite3"
        )
        self.addCleanup(replayed.close)
        self.assertEqual(
            source_rows,
            [
                dict(row)
                for row in replayed.connection.execute(
                    "SELECT * FROM receipts ORDER BY id"
                )
            ],
        )

        current_receipt = reopened.connection.execute(
            "SELECT id FROM receipts WHERE ledger_seq = 2"
        ).fetchone()
        with reopened._authorized_writes():
            reopened.connection.execute("DROP TRIGGER receipts_no_update")
            reopened.connection.execute(
                "UPDATE receipts SET document = NULL, proposer = NULL, "
                "schema_version = NULL WHERE id = ?",
                (current_receipt["id"],),
            )
        downgraded = reopened.verify_ledger()
        self.assertFalse(downgraded["valid"])
        self.assertIn(
            f"RECEIPT_DOCUMENT_MISMATCH:{current_receipt['id']}",
            downgraded["errors"],
        )

    def test_rejected_only_legacy_prefix_precedes_first_schema_1_2_entry(
        self,
    ) -> None:
        empty_legacy_root = sha256_json(
            {table: [] for table in ("sources", "claims", "evidence", "conflicts")}
        )
        connection = sqlite3.connect(self.database, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM receipts WHERE ledger_seq IS NOT NULL")
            connection.execute("DELETE FROM ledger")
            connection.execute("DELETE FROM evidence")
            connection.execute("DELETE FROM claims")
            connection.execute("DELETE FROM sources")
            connection.execute(
                "UPDATE receipts SET state_root = ?", (empty_legacy_root,)
            )
        finally:
            connection.close()

        staged = Kernel(self.database, self.registry)
        staged.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]),
            (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes(),
        )
        self._rewrite_accepted_as_schema_1_2(staged, ledger_seq=1)
        with staged._authorized_writes():
            staged.connection.execute(
                "DELETE FROM kernel_metadata WHERE name = ?",
                (Kernel._LEGACY_RECEIPT_PIN_NAME,),
            )
        staged.close()

        reopened = Kernel(self.database, self.registry)
        self.addCleanup(reopened.close)
        source_rows = [
            dict(row)
            for row in reopened.connection.execute(
                "SELECT * FROM receipts ORDER BY id"
            )
        ]

        self.assertTrue(reopened.verify_ledger()["valid"])
        self.assertEqual(
            ["VALIDATION_ERROR", "TRANSACTION_CONFLICT", "COMMITTED"],
            [row["outcome"] for row in source_rows],
        )
        self.assertEqual(
            [None, None, "1.2.0"],
            [row["schema_version"] for row in source_rows],
        )
        replayed = reopened.replay(
            Path(self.temp.name) / "rejected-prefix-replay.sqlite3"
        )
        self.addCleanup(replayed.close)
        self.assertEqual(
            source_rows,
            [
                dict(row)
                for row in replayed.connection.execute(
                    "SELECT * FROM receipts ORDER BY id"
                )
            ],
        )
        self.assertTrue(replayed.verify_ledger()["valid"])

    def test_direct_rejected_only_legacy_database_replays_exactly(self) -> None:
        empty_legacy_root = sha256_json(
            {table: [] for table in ("sources", "claims", "evidence", "conflicts")}
        )
        connection = sqlite3.connect(self.database, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM receipts WHERE ledger_seq IS NOT NULL")
            connection.execute("DELETE FROM ledger")
            connection.execute("DELETE FROM evidence")
            connection.execute("DELETE FROM claims")
            connection.execute("DELETE FROM sources")
            connection.execute(
                "UPDATE receipts SET state_root = ?", (empty_legacy_root,)
            )
        finally:
            connection.close()

        reopened = Kernel(self.database, self.registry)
        self.addCleanup(reopened.close)
        source_rows = [
            dict(row)
            for row in reopened.connection.execute(
                "SELECT * FROM receipts ORDER BY id"
            )
        ]

        self.assertEqual(0, reopened.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
        self.assertEqual(
            ["VALIDATION_ERROR", "TRANSACTION_CONFLICT"],
            [row["outcome"] for row in source_rows],
        )
        self.assertTrue(reopened.verify_ledger()["valid"])
        replayed = reopened.replay(
            Path(self.temp.name) / "direct-rejected-only-replay.sqlite3"
        )
        self.addCleanup(replayed.close)
        self.assertEqual(
            source_rows,
            [
                dict(row)
                for row in replayed.connection.execute(
                    "SELECT * FROM receipts ORDER BY id"
                )
            ],
        )
        self.assertTrue(replayed.verify_ledger()["valid"])

    def test_schema_1_1_full_events_remain_readable_without_exact_documents(
        self,
    ) -> None:
        database = Path(self.temp.name) / "schema-1.1.sqlite3"
        kernel = Kernel(database, self.registry)
        source = copy.deepcopy(self.objects["source_revision_postgresql"])
        content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        kernel.register_source(source, content)
        row = kernel.connection.execute(
            "SELECT * FROM ledger WHERE seq = 1"
        ).fetchone()
        proposal = json.loads(row["proposal"])
        proposal["versions"]["schema"] = "1.1.0"
        proposal["versions"]["projection"] = "markdown-projection@2"
        events = json.loads(row["events"])
        proposal_hash = sha256_json(proposal)
        entry_hash = sha256_json(
            Kernel._ledger_envelope(
                seq=1,
                prev_hash=None,
                proposal_hash=proposal_hash,
                pre_state_root=row["pre_state_root"],
                post_state_root=row["state_root"],
                versions=proposal["versions"],
                events=events,
                committed_at=row["committed_at"],
            )
        )
        with kernel._authorized_writes():
            kernel.connection.execute("DROP TRIGGER ledger_no_update")
            kernel.connection.execute("DROP TRIGGER receipts_no_update")
            kernel.connection.execute(
                """UPDATE ledger
                   SET proposal = ?, proposal_hash = ?, entry_hash = ?, document = NULL
                   WHERE seq = 1""",
                (canonical_json(proposal), proposal_hash, entry_hash),
            )
            kernel.connection.execute(
                "UPDATE receipts SET proposal_hash = ?, document = NULL, "
                "schema_version = '1.1.0'",
                (proposal_hash,),
            )
        kernel.close()

        reopened = Kernel(database, self.registry)
        self.addCleanup(reopened.close)
        verification = reopened.verify_ledger()
        replayed = reopened.replay(Path(self.temp.name) / "schema-1.1-replay.sqlite3")
        self.addCleanup(replayed.close)

        self.assertTrue(verification["valid"], verification["errors"])
        self.assertEqual(reopened.state_root(), replayed.state_root())
        self.assertTrue(replayed.verify_ledger()["valid"])
        with self.assertRaises(ValidationFailure) as raised:
            reopened.ledger_entries()
        self.assertEqual(
            "LEGACY_LEDGER_CONTRACT_INCOMPLETE:1", raised.exception.code
        )
        with self.assertRaises(ValidationFailure) as raised:
            reopened.decision_receipts()
        self.assertEqual(
            "LEGACY_RECEIPT_CONTRACT_INCOMPLETE:1", raised.exception.code
        )

        with reopened._authorized_writes():
            reopened.connection.execute("DROP TRIGGER receipts_no_update")
            reopened.connection.execute(
                "UPDATE receipts SET document = '{}', schema_version = '1.2.0' "
                "WHERE id = 1"
            )

        corrupted = reopened.verify_ledger()
        self.assertFalse(corrupted["valid"])
        self.assertIn("RECEIPT_DOCUMENT_MISMATCH:1", corrupted["errors"])
        with self.assertRaises(ValidationFailure) as raised:
            reopened.decision_receipts()
        self.assertEqual(
            "LEGACY_RECEIPT_CONTRACT_INCOMPLETE:1", raised.exception.code
        )

    def _write_baseline_database(self) -> dict[str, Any]:
        """Write the on-disk schema and hash envelope used at commit 3c3cdf0."""

        source = copy.deepcopy(self.objects["source_revision_postgresql"])
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["versions"]["schema"] = BASELINE_SCHEMA_VERSION
        proposal["versions"]["projection"] = "markdown-projection@1"
        proposal["versions"].pop("predicate_registry_hash", None)
        operation = proposal["operations"][0]
        claim = operation["claim"]
        evidence = operation["initial_evidence"]
        content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        self.assertEqual(source["content_hash"], sha256_bytes(content))

        connection = sqlite3.connect(self.database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
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
            connection.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?)",
                (
                    source["revision_id"],
                    source["content_hash"],
                    canonical_json(source),
                    content,
                ),
            )
            connection.execute(
                "INSERT INTO claims VALUES (?, ?, ?, ?, 'ACTIVE', 1, NULL)",
                (
                    claim["claim_id"],
                    claim["proposition_hash"],
                    canonical_json(claim["proposition"]),
                    canonical_json(claim),
                ),
            )
            for link in evidence:
                connection.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?, ?)",
                    (
                        link["evidence_link_id"],
                        link["claim_id"],
                        link["source_revision_id"],
                        canonical_json(link),
                    ),
                )

            state_root = self._baseline_state_root(connection)
            self.assertEqual(BASELINE_STATE_ROOT, state_root)
            events = [{"type": "CLAIM_ASSERTED", "claim_id": claim["claim_id"]}]
            proposal_hash = sha256_json(proposal)
            envelope = {
                "prev_hash": None,
                "proposal_hash": proposal_hash,
                "events": events,
                "state_root": state_root,
            }
            entry = {
                "prev_hash": None,
                "entry_hash": sha256_json(envelope),
                "proposal_hash": proposal_hash,
                "proposal": canonical_json(proposal),
                "events": canonical_json(events),
                "state_root": state_root,
            }
            connection.execute(
                """
                INSERT INTO ledger(
                  prev_hash, entry_hash, proposal_hash, proposal, events, state_root
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(entry.values()),
            )
            connection.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal["idempotency_key"],
                    proposal_hash,
                    proposal["proposal_id"],
                    "COMMITTED",
                    "[]",
                    1,
                    state_root,
                    "[]",
                ),
            )
            connection.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-validation-rejected-001",
                    "sha256:" + "7" * 64,
                    "proposal_legacy_validation_rejected_001",
                    "VALIDATION_ERROR",
                    '["SOURCE_REVISION_NOT_FOUND"]',
                    None,
                    state_root,
                    "[]",
                ),
            )
            connection.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-conflict-rejected-001",
                    "sha256:" + "8" * 64,
                    "proposal_legacy_conflict_rejected_001",
                    "TRANSACTION_CONFLICT",
                    '["BASE_STATE_ROOT_MISMATCH"]',
                    None,
                    state_root,
                    "[]",
                ),
            )
            return entry
        finally:
            connection.close()

    def _rewrite_accepted_as_schema_1_2(
        self, kernel: Kernel, *, ledger_seq: int
    ) -> None:
        ledger = kernel.connection.execute(
            "SELECT * FROM ledger WHERE seq = ?", (ledger_seq,)
        ).fetchone()
        proposal = json.loads(ledger["proposal"])
        proposal["versions"]["schema"] = "1.2.0"
        proposal_hash = sha256_json(proposal)
        events = json.loads(ledger["events"])
        entry_hash = sha256_json(
            Kernel._ledger_envelope(
                seq=ledger_seq,
                prev_hash=ledger["prev_hash"],
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
            "SELECT * FROM receipts WHERE ledger_seq = ?", (ledger_seq,)
        ).fetchone()
        receipt_document = json.loads(receipt["document"])
        receipt_document.pop("proposer")
        receipt_document["proposal_hash"] = proposal_hash
        receipt_document["head_after"] = entry_hash
        with kernel._authorized_writes():
            kernel.connection.execute("DROP TRIGGER ledger_no_update")
            kernel.connection.execute("DROP TRIGGER receipts_no_update")
            kernel.connection.execute(
                "UPDATE ledger SET proposal = ?, proposal_hash = ?, "
                "entry_hash = ?, document = ? WHERE seq = ?",
                (
                    canonical_json(proposal),
                    proposal_hash,
                    entry_hash,
                    canonical_json(ledger_document),
                    ledger_seq,
                ),
            )
            kernel.connection.execute(
                "UPDATE receipts SET proposal_hash = ?, proposer = NULL, "
                "document = ?, schema_version = '1.2.0' WHERE ledger_seq = ?",
                (
                    proposal_hash,
                    canonical_json(receipt_document),
                    ledger_seq,
                ),
            )
            kernel.connection._PublicConnection__connection.executescript(
                """
                CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
                BEGIN SELECT RAISE(ABORT, 'LEDGER_APPEND_ONLY'); END;
                CREATE TRIGGER receipts_no_update BEFORE UPDATE ON receipts
                BEGIN SELECT RAISE(ABORT, 'RECEIPT_APPEND_ONLY'); END;
                """
            )

    @staticmethod
    def _baseline_state_root(connection: sqlite3.Connection) -> str:
        state: dict[str, list[dict[str, Any]]] = {}
        for table in ("sources", "claims", "evidence", "conflicts"):
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            normalized = []
            for row in rows:
                item = dict(row)
                if table == "sources":
                    item["content"] = sha256_bytes(bytes(item["content"]))
                normalized.append(item)
            state[table] = normalized
        return sha256_json(state)


if __name__ == "__main__":
    unittest.main()
