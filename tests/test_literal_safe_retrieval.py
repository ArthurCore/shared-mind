from __future__ import annotations

import io
import json

from shared_mind.product_cli import main as product_cli_main
from shared_mind.product_mcp_server import ProductMcpApplication
from shared_mind.retrieval import RETRIEVAL_INDEX_VERSION

from tests.product_support import ProductTestCase


LITERAL_SEARCH_SOURCE = """\
# Literal retrieval evidence

DEV-088 makes task identifiers searchable without exposing FTS syntax.
The current compatibility target is schema 1.3 and retrieval-index@1.
OR, NOT, and NEAR are ordinary words in this source rather than query operators.
Quoted, parenthesized, C++, and Korean task label 한글-검색 are evidence text.
"""


class LiteralSafeRetrievalTest(ProductTestCase):
    def _build_literal_index(self) -> None:
        source = self.write_source("literal-search.md", LITERAL_SEARCH_SOURCE)
        self.service.ingest([source])
        self.service.build_indexes()

    def _source_ids(self, result: dict) -> list[str]:
        return [
            item["document_id"]
            for item in result["results"]
            if item["kind"] == "SOURCE_TEXT"
        ]

    def _product_cli(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        code = product_cli_main(
            ["--workspace", str(self.workspace_root), *args], stdout=output
        )
        return code, json.loads(output.getvalue())

    def test_task_ids_and_versions_are_literal_search_terms(self) -> None:
        self._build_literal_index()

        task = self.service.search("DEV-088", kinds=("SOURCE_TEXT",), limit=20)
        version = self.service.search("schema 1.3", kinds=("SOURCE_TEXT",), limit=20)

        self.assertTrue(self._source_ids(task))
        self.assertEqual(self._source_ids(task), self._source_ids(version))
        self.assertEqual("retrieval-index@2", RETRIEVAL_INDEX_VERSION)
        self.assertEqual(RETRIEVAL_INDEX_VERSION, task["retrieval_version"])

    def test_fts_operators_quotes_parentheses_and_symbols_never_escape_query_data(
        self,
    ) -> None:
        self._build_literal_index()

        for query in (
            "OR NOT NEAR",
            '\"Quoted\" (parenthesized) C++',
            "한글-검색",
            "' OR 1=1 --",
            "--- ... ((( )))",
        ):
            with self.subTest(query=query):
                result = self.service.search(query, kinds=("SOURCE_TEXT",), limit=20)
                self.assertEqual("LEXICAL", result["mode"])
                self.assertIsInstance(result["results"], list)

    def test_fts_and_dependency_free_fallback_share_literal_token_semantics(self) -> None:
        self._build_literal_index()
        query = "DEV-088 schema 1.3 OR"

        fts = self.service.search(query, kinds=("SOURCE_TEXT",), limit=20)
        self.service.store._fts_enabled = False
        fallback = self.service.search(query, kinds=("SOURCE_TEXT",), limit=20)

        self.assertEqual(self._source_ids(fts), self._source_ids(fallback))
        self.assertTrue(self._source_ids(fts))

    def test_python_cli_and_mcp_return_the_same_literal_search_results(self) -> None:
        self._build_literal_index()
        query = "DEV-088 schema 1.3"
        python_result = self.service.search(
            query, kinds=("SOURCE_TEXT",), limit=20
        )

        code, cli = self._product_cli(
            "search", query, "--kind", "SOURCE_TEXT", "--limit", "20"
        )
        self.assertEqual(0, code)
        self.assertEqual("SEARCH_COMPLETED", cli["code"])

        app = ProductMcpApplication(self.workspace)
        try:
            mcp = app.call_tool(
                "search",
                {"query": query, "kinds": ["SOURCE_TEXT"], "limit": 20},
            )
        finally:
            app.close()
        self.assertFalse(mcp["isError"])

        expected = self._source_ids(python_result)
        self.assertEqual("retrieval-index@2", python_result["retrieval_version"])
        self.assertEqual(
            python_result["retrieval_version"], cli["data"]["retrieval_version"]
        )
        self.assertEqual(
            python_result["retrieval_version"],
            mcp["structuredContent"]["data"]["retrieval_version"],
        )
        self.assertEqual(
            expected,
            self._source_ids(cli["data"]),
        )
        self.assertEqual(
            expected,
            self._source_ids(mcp["structuredContent"]["data"]),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
