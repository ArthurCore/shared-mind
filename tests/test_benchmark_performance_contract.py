from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared_mind import Kernel


ROOT = Path(__file__).resolve().parents[1]


class BenchmarkQueryPlanContractTest(unittest.TestCase):
    """Structural performance gates without machine-dependent timing limits."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", registry)
        self.addCleanup(self.kernel.close)

    def test_receipt_schema_indexes_idempotency_lookup(self) -> None:
        indexes = self.kernel.connection.execute(
            """
            SELECT name, sql FROM sqlite_master
            WHERE type = 'index' AND tbl_name = 'receipts'
            ORDER BY name
            """
        ).fetchall()
        index_definitions = [str(index["sql"] or "").lower() for index in indexes]

        self.assertTrue(
            any("idempotency_key" in definition for definition in index_definitions),
            index_definitions,
        )

    def test_large_receipt_idempotency_lookup_uses_indexed_search(self) -> None:
        with self.kernel._authorized_writes():
            for index in range(5_000):
                self.kernel.connection.execute(
                    """
                    INSERT INTO receipts(
                      idempotency_key, proposal_hash, proposal_id, outcome,
                      reason_codes, ledger_seq, state_root, conflict_ids,
                      document, schema_version
                    ) VALUES (?, ?, ?, 'VALIDATION_ERROR', '[]', NULL, ?, '[]', NULL, ?)
                    """,
                    (
                        f"benchmark-receipt-{index:08d}",
                        "sha256:" + f"{index:064x}",
                        f"proposal_benchmark_receipt_{index:08d}",
                        "sha256:" + "0" * 64,
                        Kernel.SUPPORTED_VERSIONS["schema"],
                    ),
                )

        plan = self.kernel.connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM receipts
            WHERE idempotency_key = ?
            ORDER BY id
            LIMIT 1
            """,
            ("benchmark-receipt-00004999",),
        ).fetchall()
        details = [str(row["detail"]).upper() for row in plan]

        self.assertTrue(
            any("SEARCH RECEIPTS" in detail for detail in details), details
        )
        self.assertFalse(
            any("SCAN RECEIPTS" in detail for detail in details), details
        )


if __name__ == "__main__":
    unittest.main()
