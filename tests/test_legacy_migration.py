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
        self.assertEqual("markdown-projection@2", projection["projection_version"])
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
            return entry
        finally:
            connection.close()

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
