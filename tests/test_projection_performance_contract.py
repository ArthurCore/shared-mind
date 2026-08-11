from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from shared_mind.canonical import canonical_json
from shared_mind.projection import build_context_pack


class ProjectionQueryPlanContractTest(unittest.TestCase):
    """Machine-independent gates for the hot-active context query shape."""

    def test_context_ignores_untrusted_derived_history_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "forged-history.sqlite3"
            connection, claim_id = self._open_history_database(database)
            connection.executescript(
                """
                CREATE TABLE ledger_object_refs(
                  object_id TEXT NOT NULL,
                  seq INTEGER NOT NULL,
                  PRIMARY KEY(object_id, seq)
                ) WITHOUT ROWID;
                """
            )
            connection.executemany(
                "INSERT INTO ledger_object_refs(object_id, seq) VALUES (?, ?)",
                (
                    (claim_id, 1),
                    (claim_id, 48),
                    (claim_id, 999),
                    ("claim_forged", 999),
                ),
            )
            connection.commit()
            statements: list[str] = []
            connection.set_trace_callback(statements.append)

            pack = build_context_pack(
                connection,
                budget_bytes=4_096,
                purpose="Ignore untrusted derived history data.",
            )

            claim = pack["current_claims"][0]
            self.assertEqual(list(range(33, 49)), claim["history_sequences"])
            self.assertEqual(16, claim["history_included_count"])
            self.assertEqual(32, claim["history_omitted_count"])
            history_queries = [
                statement.lower()
                for statement in statements
                if (
                    "ledger_object_refs" in statement.lower()
                    or "json_tree" in statement.lower()
                )
            ]
            self.assertTrue(
                any("json_tree" in statement for statement in history_queries),
                history_queries,
            )
            self.assertFalse(
                any("ledger_object_refs" in statement for statement in history_queries),
                history_queries,
            )

    def test_context_history_query_scans_ledger_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "hot-active.sqlite3"
            connection, _ = self._open_history_database(database)
            statements: list[str] = []
            connection.set_trace_callback(statements.append)

            build_context_pack(
                connection,
                budget_bytes=4_096,
                purpose="Pin the bounded history query plan.",
            )

            history_queries = [
                statement
                for statement in statements
                if "json_tree" in statement.lower()
                and "from ledger" in statement.lower()
            ]
            self.assertEqual(1, len(history_queries), history_queries)
            plan = connection.execute(
                "EXPLAIN QUERY PLAN " + history_queries[0]
            ).fetchall()
            ledger_scans = [
                str(row["detail"])
                for row in plan
                if "SCAN LEDGER" in str(row["detail"]).upper()
            ]

            self.assertEqual(1, len(ledger_scans), [dict(row) for row in plan])

    def _open_history_database(
        self, database: Path
    ) -> tuple[sqlite3.Connection, str]:
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE sources(revision_id TEXT PRIMARY KEY, content BLOB);
            CREATE TABLE claims(
              claim_id TEXT PRIMARY KEY,
              document TEXT NOT NULL,
              proposition TEXT NOT NULL,
              proposition_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              version INTEGER NOT NULL,
              superseded_by TEXT
            );
            CREATE TABLE evidence(
              evidence_link_id TEXT PRIMARY KEY,
              claim_id TEXT NOT NULL,
              source_revision_id TEXT NOT NULL,
              document TEXT NOT NULL
            );
            CREATE TABLE conflicts(
              conflict_id TEXT PRIMARY KEY,
              family_key TEXT NOT NULL,
              kind TEXT NOT NULL,
              member_digest TEXT NOT NULL,
              members TEXT NOT NULL,
              status TEXT NOT NULL,
              episode INTEGER NOT NULL,
              version INTEGER,
              resolution TEXT,
              opened_seq INTEGER
            );
            CREATE TABLE ledger(
              seq INTEGER PRIMARY KEY,
              entry_hash TEXT NOT NULL,
              proposal TEXT NOT NULL,
              events TEXT NOT NULL
            );
            """
        )
        claim_id = "claim_hot_history"
        connection.execute(
            """
            INSERT INTO claims(
              claim_id, document, proposition, proposition_hash,
              status, version, superseded_by
            ) VALUES (?, ?, ?, ?, 'ACTIVE', 1, NULL)
            """,
            (
                claim_id,
                canonical_json({"claim_id": claim_id}),
                canonical_json({"subject": "benchmark"}),
                "sha256:" + "0" * 64,
            ),
        )
        for sequence in range(1, 49):
            connection.execute(
                "INSERT INTO ledger(seq, entry_hash, proposal, events) "
                "VALUES (?, ?, ?, ?)",
                (
                    sequence,
                    "sha256:" + f"{sequence:064x}",
                    canonical_json(
                        {
                            "operations": [{"claim_id": claim_id}],
                            "versions": {"schema": "1.2.0"},
                        }
                    ),
                    "[]",
                ),
            )
        connection.commit()
        return connection, claim_id


if __name__ == "__main__":
    unittest.main()
