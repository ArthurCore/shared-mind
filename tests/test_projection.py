from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json
from shared_mind.projection import (
    ContextBudgetError,
    build_context_pack,
    project_json,
    project_markdown,
)


ROOT = Path(__file__).resolve().parents[1]


class DeterministicProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text()
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text()
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", registry)
        self.kernel.register_source(
            self.objects["source_revision_postgresql"],
            (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes(),
        )

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_json_and_markdown_are_byte_stable_and_information_preserving(self) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])
        receipt = self.kernel.commit(
            self.objects["assert_mysql_same_interval_proposal"]
        )
        self.assertEqual("FACT_CONFLICT", receipt.outcome)

        first_json = project_json(self.kernel)
        second_json = project_json(self.kernel.connection)
        first_markdown = project_markdown(self.kernel)
        second_markdown = project_markdown(self.kernel.connection)

        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)
        self.assertTrue(first_json.endswith("\n"))
        self.assertTrue(first_markdown.endswith("\n"))

        projection = json.loads(first_json)
        mysql_claim_id = self.objects["assert_mysql_same_interval_proposal"][
            "operations"
        ][0]["claim"]["claim_id"]
        self.assertEqual("markdown-projection@1", projection["projection_version"])
        self.assertEqual(2, projection["ledger"]["head_sequence"])
        self.assertEqual(self.kernel.state_root(), projection["state_root"])
        self.assertEqual(
            sorted([mysql_claim_id, "claim_atlas_postgresql_001"]),
            [item["claim"]["claim_id"] for item in projection["claims"]],
        )
        self.assertEqual(2, len(projection["evidence"]))
        selector = projection["evidence"][0]["evidence_link"]["selector"]
        self.assertEqual("sha256:", selector["excerpt_hash"][:7])
        self.assertLess(selector["start_byte"], selector["end_byte"])
        self.assertEqual(
            self.objects["source_revision_postgresql"]["source_locator"],
            projection["sources"][0]["source_revision"]["source_locator"],
        )

        conflict = projection["conflicts"][0]
        self.assertEqual("OPEN", conflict["status"])
        self.assertEqual(
            sorted([mysql_claim_id, "claim_atlas_postgresql_001"]),
            conflict["members"],
        )
        self.assertEqual([2], conflict["history_sequences"])
        self.assertEqual([1], projection["claims"][1]["history_sequences"])

        for searchable_value in (
            mysql_claim_id,
            "claim_atlas_postgresql_001",
            conflict["conflict_id"],
            selector["excerpt_hash"],
            self.objects["source_revision_postgresql"]["revision_id"],
            "History: ledger sequence",
        ):
            self.assertIn(searchable_value, first_markdown)

    def test_projection_includes_future_continuity_tables_without_schema_assumptions(
        self,
    ) -> None:
        self.kernel.connection.executescript(
            """
            CREATE TABLE decision_records (
              decision_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              document TEXT NOT NULL,
              version INTEGER NOT NULL
            );
            CREATE TABLE open_questions (
              question_id TEXT PRIMARY KEY,
              payload TEXT NOT NULL
            );
            CREATE TABLE work_items (
              item_id TEXT PRIMARY KEY,
              payload TEXT NOT NULL
            );
            """
        )
        self.kernel.connection.execute(
            "INSERT INTO decision_records VALUES (?, ?, ?, ?)",
            ("decision_2", "ACTIVE", '{"title":"Ship SQLite"}', 1),
        )
        self.kernel.connection.execute(
            "INSERT INTO open_questions VALUES (?, ?)",
            ("question_1", '{"status":"OPEN","question":"Which CLI?"}'),
        )
        self.kernel.connection.execute(
            "INSERT INTO work_items VALUES (?, ?)",
            ("work_1", '{"status":"TODO","description":"Build projector"}'),
        )

        projection = json.loads(project_json(self.kernel))

        self.assertEqual(
            "decision_records", projection["continuity"]["decisions"][0]["table"]
        )
        self.assertEqual(
            '{"title":"Ship SQLite"}',
            projection["continuity"]["decisions"][0]["row"]["document"],
        )
        self.assertEqual(
            "open_questions", projection["continuity"]["questions"][0]["table"]
        )
        self.assertEqual(
            "work_items", projection["continuity"]["work_items"][0]["table"]
        )
        markdown = project_markdown(self.kernel)
        self.assertIn("decision_2", markdown)
        self.assertIn("question_1", markdown)
        self.assertIn("work_1", markdown)

    def test_ledger_projection_uses_numeric_sequence_order_past_single_digits(
        self,
    ) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])
        base = self.objects["assert_postgresql_proposal"]
        for index in range(2, 13):
            proposal = copy.deepcopy(base)
            evidence = copy.deepcopy(base["operations"][0]["initial_evidence"][0])
            evidence["evidence_link_id"] = f"evidence_sequence_{index:02d}"
            proposal["proposal_id"] = f"proposal_sequence_{index:02d}"
            proposal["idempotency_key"] = f"sequence-{index:02d}"
            proposal["operations"] = [
                {
                    "op_id": f"operation_sequence_{index:02d}",
                    "op": "ATTACH_EVIDENCE",
                    "evidence_link": evidence,
                }
            ]
            self.assertEqual("COMMITTED", self.kernel.commit(proposal).outcome)

        projection = json.loads(project_json(self.kernel))

        self.assertEqual(12, projection["ledger"]["head_sequence"])
        self.assertEqual(
            list(range(1, 13)),
            [entry["sequence"] for entry in projection["ledger"]["entries"]],
        )

    def test_context_pack_keeps_every_open_conflict_and_truncates_deterministically(
        self,
    ) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])
        self.kernel.commit(self.objects["assert_mysql_same_interval_proposal"])
        self._insert_optional_claims(20)

        first = build_context_pack(self.kernel, budget_bytes=3_500)
        second = build_context_pack(self.kernel.connection, budget_bytes=3_500)
        encoded = canonical_json(first).encode("utf-8")

        mysql_claim_id = self.objects["assert_mysql_same_interval_proposal"][
            "operations"
        ][0]["claim"]["claim_id"]
        self.assertEqual(first, second)
        self.assertLessEqual(len(encoded), 3_500)
        self.assertEqual(1, len(first["open_conflicts"]))
        self.assertEqual(
            sorted([mysql_claim_id, "claim_atlas_postgresql_001"]),
            [
                member["claim_id"]
                for member in first["open_conflicts"][0]["members"]
            ],
        )
        for member in first["open_conflicts"][0]["members"]:
            self.assertIn("proposition", member)
            self.assertTrue(member["evidence"])
            self.assertIn("projection_ref", member)
        self.assertTrue(first["truncation"]["truncated"])
        self.assertGreater(first["truncation"]["omitted_counts"]["current_claims"], 0)
        self.assertEqual(
            "project.json#/claims",
            first["truncation"]["references"][0]["projection_ref"],
        )
        self.assertEqual(len(encoded), first["truncation"]["rendered_bytes"])

    def test_token_budget_uses_a_declared_deterministic_estimator(self) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])

        context = build_context_pack(self.kernel, budget_tokens=700)
        encoded = canonical_json(context).encode("utf-8")

        self.assertLessEqual(len(encoded), 2_800)
        self.assertLessEqual(context["truncation"]["estimated_tokens"], 700)
        self.assertEqual(
            "ceil(utf8_bytes/4)",
            context["truncation"]["token_estimator"],
        )

    def test_too_small_budget_fails_instead_of_hiding_an_open_conflict(self) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])
        self.kernel.commit(self.objects["assert_mysql_same_interval_proposal"])

        with self.assertRaises(ContextBudgetError) as caught:
            build_context_pack(self.kernel, budget_bytes=64)

        self.assertEqual(64, caught.exception.budget_bytes)
        self.assertGreater(caught.exception.required_bytes, 64)
        self.assertIn("open conflicts", str(caught.exception))

    def _insert_optional_claims(self, count: int) -> None:
        template = copy.deepcopy(
            self.objects["assert_postgresql_proposal"]["operations"][0]["claim"]
        )
        for index in range(count):
            claim = copy.deepcopy(template)
            claim["claim_id"] = f"claim_optional_{index:02d}"
            claim["proposition"]["subject"]["entity_id"] = (
                f"system:optional:{index:02d}"
            )
            claim["proposition_hash"] = sha256_json(claim["proposition"])
            self.kernel.connection.execute(
                "INSERT INTO claims VALUES (?, ?, ?, ?, 'ACTIVE', 1, NULL)",
                (
                    claim["claim_id"],
                    claim["proposition_hash"],
                    canonical_json(claim["proposition"]),
                    canonical_json(claim),
                ),
            )


if __name__ == "__main__":
    unittest.main()
