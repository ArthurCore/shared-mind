from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_bytes, sha256_json
from shared_mind.projection import (
    ContextBudgetError,
    ProjectionError,
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
        self.assertEqual("markdown-projection@3", projection["projection_version"])
        self.assertEqual(3, projection["ledger"]["head_sequence"])
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
        self.assertEqual([3], conflict["history_sequences"])
        self.assertEqual([2], projection["claims"][1]["history_sequences"])

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
        empty_root = json.loads(project_json(self.kernel))["state_root"]
        with self.kernel._authorized_writes():
            self.kernel.connection.execute(
                "INSERT INTO decision_records VALUES (?, ?, ?, ?, ?)",
                (
                    "decision_2",
                    "ACTIVE",
                    1,
                    None,
                    '{"decision_id":"decision_2","status":"ACTIVE","title":"Ship SQLite","version":1}',
                ),
            )
            self.kernel.connection.execute(
                "INSERT INTO open_questions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "question_1",
                    "OPEN",
                    1,
                    None,
                    None,
                    '{"question":"Which CLI?","question_id":"question_1","status":"OPEN","version":1}',
                ),
            )
            self.kernel.connection.execute(
                "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "work_1",
                    "TODO",
                    1,
                    None,
                    "2026-08-11T00:00:00Z",
                    '{"description":"Build projector","status":"TODO","version":1,"work_item_id":"work_1"}',
                ),
            )

        projection = json.loads(project_json(self.kernel))

        self.assertNotEqual(empty_root, projection["state_root"])
        self.assertEqual(self._expected_state_root(), projection["state_root"])
        self.assertEqual(
            "decision_records", projection["continuity"]["decisions"][0]["table"]
        )
        self.assertEqual(
            '{"decision_id":"decision_2","status":"ACTIVE","title":"Ship SQLite","version":1}',
            projection["continuity"]["decisions"][0]["row"]["document"],
        )
        self.assertEqual(
            "Ship SQLite",
            projection["continuity"]["decisions"][0]["document"]["title"],
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

    def test_projection_preserves_full_ledger_conflict_and_continuity_history(self) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])
        self.kernel.commit(self.objects["assert_mysql_same_interval_proposal"])
        decision = {
            "decision_id": "decision_history_1",
            "status": "ACTIVE",
            "version": 1,
            "title": "Keep complete history",
        }
        head = self.kernel.connection.execute(
            "SELECT entry_hash, state_root FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        resolution = {
            "resolution_epoch": 1,
            "selected_claim_ids": [],
            "rejected_claim_ids": [],
        }
        with self.kernel._authorized_writes():
            self.kernel.connection.execute(
                "INSERT INTO decision_records VALUES (?, ?, ?, ?, ?)",
                (
                    decision["decision_id"],
                    decision["status"],
                    decision["version"],
                    None,
                    canonical_json(decision),
                ),
            )
            self.kernel.connection.execute(
                """INSERT INTO ledger(
                     prev_hash, entry_hash, proposal_hash, proposal, events,
                     pre_state_root, state_root, committed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    head["entry_hash"],
                    "sha256:" + "a" * 64,
                    "sha256:" + "b" * 64,
                    canonical_json(
                        {
                            "proposal_id": "proposal_history_1",
                            "versions": {"schema": "1.0.0"},
                        }
                    ),
                    canonical_json(
                        [{"event_type": "DECISION_RECORDED", "decision": decision}]
                    ),
                    head["state_root"],
                    self._expected_state_root(),
                    "2026-08-11T01:02:03Z",
                ),
            )
            self.kernel.connection.execute(
                "UPDATE conflicts SET status = 'RESOLVED', version = 2, resolution = ?",
                (canonical_json(resolution),),
            )

        projection = json.loads(project_json(self.kernel))

        ledger_entry = projection["ledger"]["entries"][-1]
        self.assertEqual(head["state_root"], ledger_entry["pre_state_root"])
        self.assertEqual("2026-08-11T01:02:03Z", ledger_entry["committed_at"])
        self.assertEqual("DECISION_RECORDED", ledger_entry["events"][0]["event_type"])
        conflict = projection["conflicts"][0]
        self.assertEqual(2, conflict["version"])
        self.assertEqual(resolution, conflict["resolution"])
        self.assertEqual(3, conflict["opened_sequence"])
        decision_projection = projection["continuity"]["decisions"][0]
        self.assertEqual(decision, decision_projection["document"])
        self.assertEqual([4], decision_projection["history_sequences"])
        self.assertEqual(
            ["project.json#/ledger/entries/3"],
            decision_projection["history_refs"],
        )
        self.assertEqual(
            ledger_entry,
            self._resolve_pointer(projection, ledger_entry["projection_ref"]),
        )

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

        self.assertEqual(13, projection["ledger"]["head_sequence"])
        self.assertEqual(
            list(range(1, 14)),
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
            self.assertEqual(
                member["claim_id"],
                self._resolve_pointer(
                    json.loads(project_json(self.kernel)), member["projection_ref"]
                )["claim"]["claim_id"],
            )
            for evidence in member["evidence"]:
                self.assertEqual(
                    evidence["evidence_link_id"],
                    self._resolve_pointer(
                        json.loads(project_json(self.kernel)),
                        evidence["projection_ref"],
                    )["evidence_link_id"],
                )
        self.assertTrue(first["truncation"]["truncated"])
        self.assertGreater(first["truncation"]["omitted_counts"]["current_claims"], 0)
        self.assertEqual(
            "project.json#/claims",
            first["truncation"]["references"][0]["projection_ref"],
        )
        self.assertEqual(len(encoded), first["truncation"]["rendered_bytes"])
        self.assertEqual("context-selection@2", first["truncation"]["selection_rule_version"])
        self.assertIn("open-conflicts", first["truncation"]["selection_rule"])
        for reference in first["truncation"]["references"]:
            self._resolve_pointer(
                json.loads(project_json(self.kernel)), reference["projection_ref"]
            )

    def test_context_pack_includes_explicit_purpose_or_marks_it_missing(self) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])

        supplied = build_context_pack(
            self.kernel,
            budget_bytes=8_000,
            purpose="Preserve the project's reasoning across AI sessions.",
        )
        missing = build_context_pack(self.kernel, budget_bytes=8_000)

        self.assertEqual(
            "Preserve the project's reasoning across AI sessions.",
            supplied["purpose"],
        )
        self.assertFalse(supplied["purpose_missing"])
        self.assertIsNone(missing["purpose"])
        self.assertTrue(missing["purpose_missing"])

    def test_context_never_truncates_active_continuity_records(self) -> None:
        with self.kernel._authorized_writes():
            self.kernel.connection.execute(
                "INSERT INTO decision_records VALUES (?, ?, ?, ?, ?)",
                (
                    "decision_mandatory_1",
                    "ACTIVE",
                    1,
                    None,
                    '{"decision_id":"decision_mandatory_1","status":"ACTIVE","title":"Keep handoff state","version":1}',
                ),
            )
            self.kernel.connection.execute(
                "INSERT INTO open_questions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "question_mandatory_1",
                    "OPEN",
                    1,
                    None,
                    None,
                    '{"question":"What remains?","question_id":"question_mandatory_1","status":"OPEN","version":1}',
                ),
            )
            self.kernel.connection.execute(
                "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "work_mandatory_1",
                    "BLOCKED",
                    1,
                    "Need a decision",
                    "2026-08-11T00:00:00Z",
                    '{"blocker":"Need a decision","description":"Finish handoff","status":"BLOCKED","version":1,"work_item_id":"work_mandatory_1"}',
                ),
            )

        context = build_context_pack(self.kernel, budget_bytes=8_000)

        self.assertEqual(1, len(context["decisions"]))
        self.assertEqual(1, len(context["open_questions"]))
        self.assertEqual(1, len(context["work_items"]))
        with self.assertRaises(ContextBudgetError):
            build_context_pack(self.kernel, budget_bytes=1_500)

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
        self.assertEqual(
            "utf8-bytes-token-estimator@1",
            context["truncation"]["token_estimator_version"],
        )
        self.assertFalse(context["truncation"]["token_estimate_exact"])

    def test_too_small_budget_fails_instead_of_hiding_an_open_conflict(self) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])
        self.kernel.commit(self.objects["assert_mysql_same_interval_proposal"])

        with self.assertRaises(ContextBudgetError) as caught:
            build_context_pack(self.kernel, budget_bytes=64)

        self.assertEqual(64, caught.exception.budget_bytes)
        self.assertGreater(caught.exception.required_bytes, 64)
        self.assertIn("open conflicts", str(caught.exception))

    def test_projection_rejects_an_active_caller_transaction(self) -> None:
        self.kernel.connection.execute("BEGIN")
        try:
            with self.assertRaises(ProjectionError) as caught:
                project_json(self.kernel)
            self.assertIn("active transaction", str(caught.exception))
            self.assertTrue(self.kernel.connection.in_transaction)
        finally:
            self.kernel.connection.execute("ROLLBACK")

    def test_unknown_continuity_status_fails_closed(self) -> None:
        with self.kernel._authorized_writes():
            self.kernel.connection.execute("PRAGMA ignore_check_constraints = ON")
            self.kernel.connection.execute(
                "INSERT INTO decision_records VALUES (?, ?, ?, ?, ?)",
                (
                    "decision_unknown_1",
                    "ARCHIVED",
                    1,
                    None,
                    '{"decision_id":"decision_unknown_1","status":"ARCHIVED","version":1}',
                ),
            )

        with self.assertRaises(ProjectionError) as caught:
            project_json(self.kernel)

        self.assertIn("unknown decision status", str(caught.exception))

    def test_missing_open_conflict_member_fails_closed(self) -> None:
        self.kernel.commit(self.objects["assert_postgresql_proposal"])
        self.kernel.commit(self.objects["assert_mysql_same_interval_proposal"])
        conflict = self.kernel.connection.execute(
            "SELECT conflict_id, members FROM conflicts"
        ).fetchone()
        members = json.loads(conflict["members"])
        members.append("claim_missing_1")
        with self.kernel._authorized_writes():
            self.kernel.connection.execute(
                "UPDATE conflicts SET members = ? WHERE conflict_id = ?",
                (canonical_json(sorted(members)), conflict["conflict_id"]),
            )

        with self.assertRaises(ProjectionError) as caught:
            project_json(self.kernel)

        self.assertIn("missing member claim", str(caught.exception))

    def _insert_optional_claims(self, count: int) -> None:
        template = copy.deepcopy(
            self.objects["assert_postgresql_proposal"]["operations"][0]["claim"]
        )
        with self.kernel._authorized_writes():
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

    def _expected_state_root(self) -> str:
        state: dict[str, list[object]] = {}
        for table in ("sources", "claims", "evidence", "conflicts"):
            rows = self.kernel.connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
            state[table] = [
                dict(row)
                | (
                    {"content": sha256_bytes(bytes(row["content"]))}
                    if table == "sources"
                    else {}
                )
                for row in rows
            ]
        from shared_mind.continuity import state_rows

        state.update(state_rows(self.kernel.connection))
        return sha256_json(state)

    @staticmethod
    def _resolve_pointer(document: object, reference: str) -> object:
        prefix = "project.json#"
        if not reference.startswith(prefix):
            raise AssertionError(f"unexpected projection reference: {reference}")
        current = document
        pointer = reference[len(prefix) :]
        if not pointer:
            return current
        for token in pointer.lstrip("/").split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, list):
                current = current[int(token)]
            else:
                current = current[token]  # type: ignore[index]
        return current


if __name__ == "__main__":
    unittest.main()
