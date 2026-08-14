from __future__ import annotations

import time
import unittest

from tests.product_support import ProductTestCase


class _ReverseVectorRanker:
    ranker_id = "fixture-vector"
    ranker_version = "1"

    def rank(self, query, documents, *, limit):
        del query
        return [document["document_id"] for document in reversed(documents[:limit])]


class ProductRetrievalTest(ProductTestCase):
    def test_lexical_search_link_graph_and_source_traceability(self) -> None:
        self.seed_product()
        result = self.service.search("postgresql migration", limit=20)
        self.assertGreater(len(result["results"]), 0)
        self.assertEqual("LEXICAL", result["mode"])
        source_results = [item for item in result["results"] if item["kind"] == "SOURCE_TEXT"]
        self.assertTrue(source_results)
        self.assertIn("source_revision_id", source_results[0]["metadata"])
        links = self.service.tool_call("link_graph", {})
        self.assertGreater(len(links), 0)
        self.assertTrue(any(item["relation"] == "DERIVED_FROM" for item in links))

    def test_optional_vector_ranker_combines_with_rrf_without_becoming_required(self) -> None:
        self.seed_product()
        lexical = self.service.search("postgresql", limit=10)
        hybrid = self.service.search(
            "postgresql", limit=10, vector_ranker=_ReverseVectorRanker()
        )
        self.assertEqual("LEXICAL", lexical["mode"])
        self.assertEqual("HYBRID_RRF", hybrid["mode"])
        self.assertEqual("fixture-vector@1", hybrid["ranker"]["vector"])
        lexical_ids = {item["document_id"] for item in lexical["results"]}
        hybrid_ids = {item["document_id"] for item in hybrid["results"]}
        self.assertTrue(lexical_ids & hybrid_ids)
        self.assertLessEqual(len(hybrid["results"]), 10)
        self.assertEqual(
            [item["document_id"] for item in hybrid["results"]],
            [
                item["document_id"]
                for item in self.service.search(
                    "postgresql", limit=10, vector_ranker=_ReverseVectorRanker()
                )["results"]
            ],
        )

    def test_python_symbol_call_and_impact_index_is_rebuildable(self) -> None:
        code = self.write_source(
            "app.py",
            """def helper():\n    return 1\n\ndef caller():\n    return helper()\n\ndef alias():\n    selected = helper\n    return selected\n\nclass Service:\n    def run(self):\n        return caller()\n""",
        )
        self.service.ingest([code])
        first = self.service.build_indexes()
        self.assertGreaterEqual(first["symbols"], 4)
        helper = self.service.store.find_symbols("helper")[0]
        callers = self.service.tool_call(
            "impact_path", {"symbol_id": helper["symbol_id"], "direction": "INCOMING"}
        )
        self.assertTrue(any(edge["edge_kind"] == "CALLS" for edge in callers["edges"]))
        alias = self.service.store.find_symbols("alias")[0]
        alias_edges = self.service.store.code_edges(alias["symbol_id"])
        self.assertTrue(
            any(
                edge["edge_kind"] == "REFERENCES"
                and edge["target_symbol_id"] == helper["symbol_id"]
                for edge in alias_edges
            )
        )
        self.service.store.connection.execute("DELETE FROM code_edges")
        self.service.store.connection.execute("DELETE FROM code_symbols")
        second = self.service.build_indexes()
        self.assertEqual(first["symbols"], second["symbols"])
        self.assertEqual(first["code_edges"], second["code_edges"])

    def test_rebuild_is_deterministic_across_wall_clock_seconds(self) -> None:
        self.seed_product()
        first_report = self.service.build_indexes()
        first_state_hash = self.service.store.product_state_hash()
        first_fingerprints = [
            (row["document_id"], row["fingerprint"], row["updated_at"])
            for row in self.service.store.connection.execute(
                "SELECT document_id, fingerprint, updated_at "
                "FROM retrieval_documents ORDER BY document_id"
            )
        ]
        time.sleep(1.05)
        second_report = self.service.build_indexes()
        second_state_hash = self.service.store.product_state_hash()
        second_fingerprints = [
            (row["document_id"], row["fingerprint"], row["updated_at"])
            for row in self.service.store.connection.execute(
                "SELECT document_id, fingerprint, updated_at "
                "FROM retrieval_documents ORDER BY document_id"
            )
        ]
        self.assertEqual(first_report["fingerprint"], second_report["fingerprint"])
        self.assertEqual(first_state_hash, second_state_hash)
        self.assertEqual(first_fingerprints, second_fingerprints)

    def test_on_demand_capabilities_and_source_span(self) -> None:
        self.seed_product()
        capabilities = self.service.tool_call("capabilities", {})
        self.assertIn("read_source_span", capabilities["tools"])
        source = next(
            record
            for record in self.service.views.atomic_records()
            if record["kind"] == "SOURCE_REVISION"
        )
        span = self.service.tool_call(
            "read_source_span",
            {
                "revision_id": source["object_id"],
                "start_byte": 0,
                "end_byte": 32,
            },
        )
        self.assertEqual(source["object_id"], span["source_revision"]["revision_id"])
        self.assertLessEqual(len(span["excerpt"].encode("utf-8")), 32)

    def test_retrieval_evaluation_reports_recall_and_traceability(self) -> None:
        self.seed_product()
        result = self.service.retrieval.evaluate(
            [
                {
                    "query": "postgresql",
                    "expected_ids": [
                        next(
                            record["object_id"]
                            for record in self.service.views.atomic_records()
                            if record["kind"] == "CLAIM"
                        )
                    ],
                }
            ]
        )
        self.assertEqual(1, result["cases"])
        self.assertGreaterEqual(result["mean_recall"], 0.0)
        self.assertEqual(1.0, result["mean_conflict_recall"])
        self.assertGreaterEqual(result["traceability_rate"], 0.0)
        self.assertGreaterEqual(result["evidence_traceability_rate"], 0.0)
        self.assertGreaterEqual(result["mean_response_bytes"], 0.0)
        self.assertGreaterEqual(result["p95_latency_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
