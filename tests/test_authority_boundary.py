from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared_mind import Kernel


ROOT = Path(__file__).resolve().parents[1]


class PublicKernelAuthorityBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text()
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text()
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        self.content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", self.registry)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_fr_010_public_connection_rejects_claim_evidence_and_conflict_dml(
        self,
    ) -> None:
        self._populate_canonical_rows()
        root_before = self.kernel.state_root()
        attempts = self._dml_attempts(
            {
                "claims": "document = '{}'",
                "evidence": "document = '{}'",
                "conflicts": "status = 'RESOLVED'",
            }
        )

        unexpectedly_allowed = self._unexpectedly_allowed(attempts)

        self.assertEqual(root_before, self.kernel.state_root())
        self.assertEqual([], unexpectedly_allowed)

    def test_fr_010_public_connection_rejects_continuity_dml(self) -> None:
        self._populate_canonical_rows()
        root_before = self.kernel.state_root()
        attempts = self._dml_attempts(
            {
                "decision_records": "document = '{}'",
                "open_questions": "document = '{}'",
                "work_items": "document = '{}'",
            }
        )

        unexpectedly_allowed = self._unexpectedly_allowed(attempts)

        self.assertEqual(root_before, self.kernel.state_root())
        self.assertEqual([], unexpectedly_allowed)

    def test_fr_010_public_connection_cannot_drop_authority_triggers(self) -> None:
        trigger = self.kernel.connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'ledger' ORDER BY name LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(trigger, "ledger append-only trigger must exist")
        trigger_name = str(trigger["name"])
        quoted_name = '"' + trigger_name.replace('"', '""') + '"'

        self.kernel.connection.execute("BEGIN")
        try:
            with self.assertRaises((sqlite3.DatabaseError, PermissionError)):
                self.kernel.connection.execute(f"DROP TRIGGER {quoted_name}")
        finally:
            if self.kernel.connection.in_transaction:
                self.kernel.connection.execute("ROLLBACK")

        still_present = self.kernel.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()
        self.assertIsNotNone(still_present)

    def test_fr_004_legacy_register_source_is_ledger_backed_and_verifiable(
        self,
    ) -> None:
        source = self.objects["source_revision_postgresql"]

        self.kernel.register_source(source, self.content)
        first_verification = self.kernel.verify_ledger()
        self.kernel.register_source(source, self.content)
        retry_verification = self.kernel.verify_ledger()

        self.assertTrue(first_verification["valid"], first_verification["errors"])
        self.assertEqual(1, first_verification["checked_entries"])
        self.assertTrue(retry_verification["valid"], retry_verification["errors"])
        self.assertEqual(1, retry_verification["checked_entries"])
        self.assertEqual(
            1,
            self.kernel.connection.execute(
                "SELECT COUNT(*) FROM sources WHERE revision_id = ?",
                (source["revision_id"],),
            ).fetchone()[0],
        )

    def _populate_canonical_rows(self) -> None:
        self.kernel.register_source(
            self.objects["source_revision_postgresql"], self.content
        )
        expected_outcomes = {
            "assert_postgresql_proposal": "COMMITTED",
            "assert_mysql_same_interval_proposal": "FACT_CONFLICT",
            "record_decision_proposal": "COMMITTED",
            "open_question_proposal": "COMMITTED",
            "create_work_item_proposal": "COMMITTED",
        }
        for name, expected in expected_outcomes.items():
            receipt = self.kernel.commit(copy.deepcopy(self.objects[name]))
            self.assertEqual(expected, receipt.outcome, (name, receipt.reason_codes))

    @staticmethod
    def _dml_attempts(update_assignments: dict[str, str]) -> list[tuple[str, str]]:
        attempts: list[tuple[str, str]] = []
        for table, assignment in update_assignments.items():
            attempts.extend(
                (
                    (f"{table}:INSERT", f"INSERT INTO {table} SELECT * FROM {table} WHERE 0"),
                    (f"{table}:UPDATE", f"UPDATE {table} SET {assignment}"),
                    (f"{table}:DELETE", f"DELETE FROM {table} WHERE 0"),
                )
            )
        return attempts

    def _unexpectedly_allowed(
        self, attempts: list[tuple[str, str]]
    ) -> list[str]:
        allowed: list[str] = []
        for label, statement in attempts:
            self.kernel.connection.execute("BEGIN")
            try:
                try:
                    self.kernel.connection.execute(statement)
                except (sqlite3.DatabaseError, PermissionError):
                    pass
                else:
                    allowed.append(label)
            finally:
                if self.kernel.connection.in_transaction:
                    self.kernel.connection.execute("ROLLBACK")
        return allowed


if __name__ == "__main__":
    unittest.main()
