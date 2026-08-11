from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json
from shared_mind.projection import build_context_pack


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "contracts" / "atlas-predicate-registry.v1.json"


class Dev021BenchmarkFixtureContractTest(unittest.TestCase):
    """Fast contract tests for the opt-in 100k context benchmark.

    Unit tests deliberately use small ledgers.  The benchmark runner owns the
    expensive 100k execution; these tests pin its fixture, statistics, and
    context-query semantics without putting a wall-clock assertion in CI.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def test_runner_uses_nearest_rank_p95(self) -> None:
        from benchmarks.context_100k import summarize_latencies_ns

        samples = list(range(20, 0, -1))

        summary = summarize_latencies_ns(samples)

        # Nearest-rank p95 for 20 observations is sorted[ceil(20 * .95) - 1].
        self.assertEqual(20, summary["sample_count"])
        self.assertEqual(1, summary["min_ns"])
        self.assertEqual(19, summary["p95_ns"])
        self.assertEqual(20, summary["max_ns"])
        with self.assertRaises(ValueError):
            summarize_latencies_ns([])

    def test_fixture_manifest_is_path_independent_and_content_addressed(self) -> None:
        from benchmarks.fixture_builder import build_benchmark_fixture

        first = build_benchmark_fixture(
            self.root / "first.sqlite3",
            ledger_entries=12,
            profile="history-heavy",
            seed=21,
        )
        second = build_benchmark_fixture(
            self.root / "second.sqlite3",
            ledger_entries=12,
            profile="history-heavy",
            seed=21,
        )

        self.assertEqual(first, second)
        self.assertEqual("dev-021-fixture@1", first["fixture_version"])
        self.assertEqual("history-heavy", first["profile"])
        self.assertEqual(21, first["seed"])
        self.assertEqual(12, first["ledger_entries"])
        self.assertEqual(12, first["table_counts"]["ledger"])
        self.assertEqual("Kernel.commit", first["generation_api"])
        self.assertEqual(Kernel.SUPPORTED_VERSIONS["schema"], first["schema_version"])
        self.assertEqual(
            Kernel.SUPPORTED_VERSIONS["projection"], first["projection_version"]
        )
        manifest_payload = {
            key: value for key, value in first.items() if key != "manifest_hash"
        }
        self.assertEqual(sha256_json(manifest_payload), first["manifest_hash"])
        rendered = canonical_json(first)
        self.assertNotIn(str(self.root / "first.sqlite3"), rendered)
        self.assertNotIn(str(self.root / "second.sqlite3"), rendered)

    def test_small_fixture_uses_public_commits_and_verifies_and_replays(self) -> None:
        from benchmarks.fixture_builder import build_benchmark_fixture

        database = self.root / "small.sqlite3"
        observed_proposal_ids: list[str] = []
        original_commit = Kernel.commit

        def observed_commit(kernel: Kernel, proposal: object):
            if isinstance(proposal, dict):
                observed_proposal_ids.append(str(proposal.get("proposal_id")))
            return original_commit(kernel, proposal)

        with mock.patch.object(Kernel, "commit", new=observed_commit):
            manifest = build_benchmark_fixture(
                database,
                ledger_entries=16,
                profile="history-heavy",
                seed=21,
            )

        self.assertEqual(16, len(observed_proposal_ids))
        self.assertEqual(16, len(set(observed_proposal_ids)))

        kernel = Kernel(database, self.registry)
        self.addCleanup(kernel.close)
        verification = kernel.verify_ledger()
        self.assertTrue(verification["valid"], verification["errors"])
        self.assertEqual(16, verification["checked_entries"])
        self.assertEqual(manifest["head_entry_hash"], verification["head_hash"])
        self.assertEqual(manifest["state_root"], verification["state_root"])
        self.assertEqual(
            16,
            kernel.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
        )

        replayed = kernel.replay(self.root / "replayed.sqlite3")
        self.addCleanup(replayed.close)
        self.assertEqual(kernel.state_root(), replayed.state_root())
        self.assertEqual(
            16,
            replayed.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
        )

    def test_context_query_does_not_select_every_ledger_column(self) -> None:
        database = self.root / "query-shape.sqlite3"
        kernel = Kernel(database, self.registry)
        kernel.close()
        statements: list[str] = []
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(statements.append)

        build_context_pack(
            connection,
            budget_bytes=4_096,
            purpose="Measure the DEV-021 context read path.",
        )

        normalized = [
            re.sub(r"\s+", " ", statement.replace('"', "")).strip().upper()
            for statement in statements
        ]
        full_ledger_reads = [
            statement
            for statement in normalized
            if re.search(r"\bSELECT \* FROM LEDGER\b", statement)
        ]
        self.assertEqual([], full_ledger_reads)
        unbounded_ledger_reads = [
            statement
            for statement in normalized
            if statement.startswith("SELECT ")
            and re.search(r"\bFROM LEDGER\b", statement)
            and " LIMIT " not in statement
            and " WHERE " not in statement
            and " JOIN " not in statement
            and "COUNT(" not in statement
        ]
        self.assertEqual([], unbounded_ledger_reads)

    def test_hot_active_history_is_bounded_with_a_full_projection_reference(self) -> None:
        from benchmarks.fixture_builder import build_benchmark_fixture

        database = self.root / "hot-active.sqlite3"
        manifest = build_benchmark_fixture(
            database,
            ledger_entries=96,
            profile="hot-active",
            seed=21,
        )

        context = build_context_pack(
            database,
            budget_bytes=4_096,
            purpose="Resume the hot work item without embedding its entire history.",
        )

        rendered = canonical_json(context).encode("utf-8")
        self.assertLessEqual(len(rendered), 4_096)
        hot_object = manifest["hot_object"]
        record = next(
            item
            for item in context["work_items"]
            if item["row"]["work_item_id"] == hot_object["object_id"]
        )
        included = record["history_included_count"]
        omitted = record["history_omitted_count"]
        self.assertTrue(record["history_truncated"])
        self.assertGreater(included, 0)
        self.assertGreater(omitted, 0)
        self.assertEqual(hot_object["history_entries"], included + omitted)
        self.assertEqual(included, len(record["history_sequences"]))
        self.assertEqual(included, len(record["history_refs"]))
        self.assertEqual(
            list(
                range(
                    hot_object["history_entries"] - included + 1,
                    hot_object["history_entries"] + 1,
                )
            ),
            record["history_sequences"],
        )
        self.assertEqual(record["projection_ref"], record["full_history_ref"])
        self.assertTrue(context["truncation"]["truncated"])
        self.assertTrue(
            any(
                reference["projection_ref"] == record["full_history_ref"]
                and reference["omitted_count"] == omitted
                for reference in context["truncation"]["references"]
            )
        )


if __name__ == "__main__":
    unittest.main()
