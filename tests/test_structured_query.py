from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared_mind import Kernel
from shared_mind.query import QUERY_VERSION, QuerySpec, query


ROOT = Path(__file__).resolve().parents[1]

KIND_ORDER = (
    "SOURCE_REVISION",
    "CLAIM",
    "EVIDENCE_LINK",
    "CONFLICT",
    "DECISION_RECORD",
    "OPEN_QUESTION",
    "WORK_ITEM",
)


class StructuredQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )
        fixtures = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.objects = {
            item["name"]: item["object"] for item in fixtures["typed_objects"]
        }
        self.kernel = Kernel(Path(self.temp.name) / "query.sqlite3", registry)
        content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        self.kernel.register_source(
            copy.deepcopy(self.objects["source_revision_postgresql"]), content
        )
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(
                copy.deepcopy(self.objects["assert_postgresql_proposal"])
            ).outcome,
        )
        conflict_receipt = self.kernel.commit(
            copy.deepcopy(self.objects["assert_mysql_same_interval_proposal"])
        )
        self.assertEqual("FACT_CONFLICT", conflict_receipt.outcome)
        self.assertEqual(1, len(conflict_receipt.conflict_ids))
        self.conflict_id = conflict_receipt.conflict_ids[0]
        for name in (
            "record_decision_proposal",
            "open_question_proposal",
            "create_work_item_proposal",
        ):
            with self.subTest(seed_proposal=name):
                self.assertEqual(
                    "COMMITTED",
                    self.kernel.commit(copy.deepcopy(self.objects[name])).outcome,
                )

        self.source = self.objects["source_revision_postgresql"]
        self.postgresql_claim = self.objects["assert_postgresql_proposal"][
            "operations"
        ][0]["claim"]
        self.postgresql_evidence = self.objects["assert_postgresql_proposal"][
            "operations"
        ][0]["initial_evidence"][0]
        self.mysql_claim = self.objects["assert_mysql_same_interval_proposal"][
            "operations"
        ][0]["claim"]
        self.decision = self.objects["record_decision_proposal"]["operations"][0][
            "decision"
        ]
        self.question = self.objects["open_question_proposal"]["operations"][0][
            "question"
        ]
        self.work_item = self.objects["create_work_item_proposal"]["operations"][
            0
        ]["work_item"]

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_exact_ids_return_one_record_from_each_of_the_seven_public_kinds(
        self,
    ) -> None:
        expected = {
            ("SOURCE_REVISION", self.source["revision_id"]),
            ("CLAIM", self.postgresql_claim["claim_id"]),
            ("EVIDENCE_LINK", self.postgresql_evidence["evidence_link_id"]),
            ("CONFLICT", self.conflict_id),
            ("DECISION_RECORD", self.decision["decision_id"]),
            ("OPEN_QUESTION", self.question["question_id"]),
            ("WORK_ITEM", self.work_item["work_item_id"]),
        }

        result = query(
            self.kernel,
            QuerySpec(ids=tuple(identifier for _, identifier in sorted(expected))),
        )

        self.assertEqual(QUERY_VERSION, result.query_version)
        self.assertEqual("markdown-projection@3", result.projection_version)
        self.assertEqual(self.kernel.state_root(), result.state_root)
        self.assertEqual(self._ledger_count(), result.ledger_sequence)
        self.assertEqual(7, result.total_matches)
        self.assertFalse(result.truncated)
        self.assertEqual(
            expected,
            {(hit["object_type"], hit["object_id"]) for hit in result.hits},
        )
        for hit in result.hits:
            self.assertEqual(
                {
                    "object_type",
                    "object_id",
                    "projection_ref",
                    "matched_fields",
                    "summary",
                    "record",
                },
                set(hit),
            )
            self.assertTrue(hit["projection_ref"].startswith("project.json#/"))
            self.assertIsInstance(hit["record"], dict)

    def test_exact_predicate_source_and_status_filters_and_title_substring(
        self,
    ) -> None:
        predicate = self.postgresql_claim["proposition"]["predicate"]
        revision_id = self.source["revision_id"]

        claims = query(
            self.kernel,
            QuerySpec(
                kinds=("CLAIM",),
                predicates=(predicate,),
                source_revision_ids=(revision_id,),
                statuses=("ACTIVE",),
            ),
        )
        source = query(
            self.kernel,
            QuerySpec(
                kinds=("SOURCE_REVISION",),
                source_ids=(self.source["source_id"],),
            ),
        )
        evidence = query(
            self.kernel,
            QuerySpec(
                kinds=("EVIDENCE_LINK",),
                source_revision_ids=(revision_id,),
            ),
        )
        titled = query(
            self.kernel,
            QuerySpec(
                kinds=("SOURCE_REVISION", "DECISION_RECORD"),
                title_contains="production",
            ),
        )

        self.assertEqual(
            {self.postgresql_claim["claim_id"], self.mysql_claim["claim_id"]},
            {hit["object_id"] for hit in claims.hits},
        )
        self.assertEqual([self.source["revision_id"]], self._hit_ids(source))
        self.assertEqual(2, evidence.total_matches)
        self.assertEqual(
            {
                self.postgresql_evidence["evidence_link_id"],
                self.objects["assert_mysql_same_interval_proposal"]["operations"][
                    0
                ]["initial_evidence"][0]["evidence_link_id"],
            },
            set(self._hit_ids(evidence)),
        )
        self.assertEqual(
            {self.source["revision_id"], self.decision["decision_id"]},
            set(self._hit_ids(titled)),
        )

    def test_filter_categories_are_anded_and_values_within_a_category_are_ored(
        self,
    ) -> None:
        result = query(
            self.kernel,
            QuerySpec(
                kinds=("CLAIM", "DECISION_RECORD"),
                ids=(
                    self.source["revision_id"],
                    self.postgresql_claim["claim_id"],
                    self.mysql_claim["claim_id"],
                    self.decision["decision_id"],
                ),
                statuses=("ACTIVE",),
            ),
        )

        self.assertEqual(
            {
                self.postgresql_claim["claim_id"],
                self.mysql_claim["claim_id"],
                self.decision["decision_id"],
            },
            set(self._hit_ids(result)),
        )
        self.assertNotIn(self.source["revision_id"], self._hit_ids(result))

        narrowed = query(
            self.kernel,
            QuerySpec(
                kinds=("CLAIM",),
                ids=(self.postgresql_claim["claim_id"],),
                predicates=(self.postgresql_claim["proposition"]["predicate"],),
                source_revision_ids=(self.source["revision_id"],),
                statuses=("ACTIVE",),
            ),
        )
        self.assertEqual([self.postgresql_claim["claim_id"]], self._hit_ids(narrowed))

    def test_unfiltered_results_have_stable_kind_id_order_and_deterministic_pages(
        self,
    ) -> None:
        full = query(self.kernel, QuerySpec())
        repeated = query(self.kernel.connection, QuerySpec())

        self.assertEqual(full, repeated)
        ordered = [(hit["object_type"], hit["object_id"]) for hit in full.hits]
        kind_index = {kind: index for index, kind in enumerate(KIND_ORDER)}
        self.assertEqual(
            sorted(ordered, key=lambda item: (kind_index[item[0]], item[1])),
            ordered,
        )
        self.assertEqual(9, full.total_matches)

        pages = [
            query(self.kernel, QuerySpec(limit=3, offset=offset))
            for offset in (0, 3, 6)
        ]
        self.assertEqual(
            list(full.hits),
            [hit for page in pages for hit in page.hits],
        )
        self.assertEqual([True, True, False], [page.truncated for page in pages])
        self.assertTrue(
            all(page.total_matches == full.total_matches for page in pages)
        )

    def test_claim_hits_preserve_fact_conflict_linkage_and_both_members(self) -> None:
        claim_result = query(
            self.kernel,
            QuerySpec(
                kinds=("CLAIM",),
                ids=(self.mysql_claim["claim_id"],),
            ),
        )
        conflict_result = query(
            self.kernel,
            QuerySpec(kinds=("CONFLICT",), ids=(self.conflict_id,)),
        )

        self.assertEqual([self.conflict_id], claim_result.hits[0]["record"]["conflict_ids"])
        conflict = conflict_result.hits[0]["record"]
        self.assertEqual("OPEN", conflict["status"])
        self.assertEqual(
            sorted(
                [
                    self.postgresql_claim["claim_id"],
                    self.mysql_claim["claim_id"],
                ]
            ),
            conflict["members"],
        )

    def test_continuity_records_are_searchable_by_id_status_and_title(self) -> None:
        result = query(
            self.kernel,
            QuerySpec(
                kinds=("DECISION_RECORD", "OPEN_QUESTION", "WORK_ITEM"),
                ids=(
                    self.decision["decision_id"],
                    self.question["question_id"],
                    self.work_item["work_item_id"],
                ),
                statuses=("ACTIVE", "OPEN", "TODO"),
            ),
        )

        self.assertEqual(
            {
                self.decision["decision_id"],
                self.question["question_id"],
                self.work_item["work_item_id"],
            },
            set(self._hit_ids(result)),
        )
        documents = {
            hit["object_type"]: hit["record"]["document"] for hit in result.hits
        }
        self.assertEqual(self.decision["title"], documents["DECISION_RECORD"]["title"])
        self.assertEqual(self.question["question"], documents["OPEN_QUESTION"]["question"])
        self.assertEqual(
            self.work_item["description"], documents["WORK_ITEM"]["description"]
        )

        title = query(
            self.kernel,
            QuerySpec(
                kinds=("DECISION_RECORD",),
                title_contains="PostgreSQL as the production",
            ),
        )
        self.assertEqual([self.decision["decision_id"]], self._hit_ids(title))

    def test_queries_are_read_only_and_leave_no_caller_owned_transaction(self) -> None:
        tables = (
            "sources",
            "claims",
            "evidence",
            "conflicts",
            "decision_records",
            "open_questions",
            "work_items",
            "ledger",
            "receipts",
        )
        before_counts = {
            table: self.kernel.connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in tables
        }
        before_root = self.kernel.state_root()
        before_head = self.kernel.connection.execute(
            "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]

        result = query(
            self.kernel,
            QuerySpec(
                kinds=("CLAIM", "CONFLICT"),
                statuses=("ACTIVE", "OPEN"),
            ),
        )

        after_counts = {
            table: self.kernel.connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in tables
        }
        after_head = self.kernel.connection.execute(
            "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]
        self.assertGreater(result.total_matches, 0)
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(before_root, self.kernel.state_root())
        self.assertEqual(before_head, after_head)
        self.assertFalse(self.kernel.connection.in_transaction)

    def test_invalid_kind_limits_offset_empty_filters_and_unknown_keys_fail_closed(
        self,
    ) -> None:
        invalid_specs = (
            lambda: QuerySpec(kinds=("LEDGER_ENTRY",)),
            lambda: QuerySpec(limit=0),
            lambda: QuerySpec(limit=1001),
            lambda: QuerySpec(offset=-1),
            lambda: QuerySpec(ids=("",)),
            lambda: QuerySpec(predicates=("",)),
            lambda: QuerySpec(source_ids=("",)),
            lambda: QuerySpec(source_revision_ids=("",)),
            lambda: QuerySpec(statuses=("",)),
            lambda: QuerySpec(title_contains=""),
        )
        for invalid_spec in invalid_specs:
            with self.subTest(spec=invalid_spec):
                with self.assertRaises(ValueError):
                    query(self.kernel, invalid_spec())

        with self.assertRaises(ValueError):
            query(self.kernel, {"kinds": ["CLAIM"], "unknown_filter": True})

    @staticmethod
    def _hit_ids(result: Any) -> list[str]:
        return [hit["object_id"] for hit in result.hits]

    def _ledger_count(self) -> int:
        return int(
            self.kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
