from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from shared_mind.memory_views import ContextRouter, MemoryViewError

from tests.product_support import ProductTestCase


class MemoryViewsProductTest(ProductTestCase):
    def test_context_drops_optional_trace_when_final_counters_cross_budget(self) -> None:
        workspace = Mock(purpose="Test project")
        kernel = Mock()
        workspace.open_kernel.return_value = kernel
        store = Mock()
        store.list_artifacts.return_value = []
        router = ContextRouter(workspace, store)
        router.views = Mock()
        router.views.projection.return_value = {
            "state_root": "sha256:" + "0" * 64,
            "ledger": {"head_sequence": 0},
        }
        router.views.atomic_records.return_value = [
            {
                "object_id": f"work_{index:04d}",
                "kind": "WORK_ITEM",
                "title": f"Work item {index}",
                "summary": "continue " + "x" * 80,
                "status": "TODO",
                "source_revision_ids": [],
                "related_ids": [],
                "projection_ref": (
                    f"project.json#/continuity/work_items/{index}"
                ),
                "document": {
                    "priority": "P1",
                    "description": "continue " + "y" * 180,
                },
            }
            for index in range(150)
        ]
        request = {
            "task": "continue work",
            "purpose": None,
            "query": "continue",
            "references": [],
            "depth": "EVIDENCE",
            "budget_bytes": 1_580,
            "budget_tokens": None,
            "hints": {},
        }

        with (
            patch(
                "shared_mind.memory_views.build_context_pack",
                return_value={"core": "z" * 200},
            ),
            patch("shared_mind.memory_views.select_skills", return_value=[]),
        ):
            result = router.route(request)

        self.assertLessEqual(result["budget"]["included_bytes"], 1_580)
        self.assertEqual(
            result["budget"]["included_bytes"],
            len(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        )

    def test_scenario_core_and_drill_down_are_derived_and_deterministic(self) -> None:
        self.seed_product()
        first = self.service.build_memory_views()
        snapshots = {
            item["artifact_id"]: (item["dependency_digest"], item["version"], item["document"])
            for item in self.service.store.list_artifacts()
        }
        second = self.service.build_memory_views()
        self.assertEqual(first["state_root"], second["state_root"])
        for item in self.service.store.list_artifacts():
            digest, version, document = snapshots[item["artifact_id"]]
            self.assertEqual(digest, item["dependency_digest"])
            self.assertEqual(version, item["version"])
            self.assertEqual(document, item["document"])
        core = self.service.store.get_artifact("artifact_core-project")
        self.assertFalse(core["document"]["authoritative"])
        self.assertEqual(first["state_root"], core["provenance"]["kernel_state_root"])
        scenario = self.service.store.get_artifact("artifact_scenario-project")
        detail = self.service.views.drill_down(scenario["artifact_id"])
        self.assertGreater(len(detail["members"]), 0)
        self.assertGreater(len(detail["history"]), 0)
        self.assertTrue(all(item["ledger_entry"] for item in detail["history"]))
        self.assertTrue(any(item["receipts"] for item in detail["history"]))
        claim = next(item for item in detail["members"] if item["kind"] == "CLAIM")
        claim_detail = self.service.views.drill_down(claim["object_id"])
        self.assertEqual(1, len(claim_detail["evidence"]))
        self.assertEqual(1, len(claim_detail["sources"]))
        self.assertEqual(1, len(claim_detail["history"]))
        self.assertTrue(claim_detail["history"][0]["proposal"])
        self.assertTrue(claim_detail["history"][0]["receipts"])

    def test_incremental_consolidation_changes_only_after_state_change(self) -> None:
        self.seed_product()
        subject = next(
            item
            for item in self.service.store.list_artifacts(artifact_type="SCENARIO")
            if item["document"]["scenario_kind"] == "SUBJECT"
        )
        subject_digest = subject["dependency_digest"]
        subject_version = subject["version"]
        unchanged = self.service.incremental_consolidation()
        self.assertEqual([], unchanged["changed_artifact_ids"])
        source = self.write_source("second.md", "WORK: P1 | Document rollback steps\n")
        batch = self.service.ingest([source])
        self.service.extract(batch["batch_id"])
        self.service.commit_batch_drafts(batch["batch_id"])
        changed = self.service.incremental_consolidation()
        self.assertIn("artifact_core-project", changed["changed_artifact_ids"])
        self.assertIn("artifact_scenario-project", changed["changed_artifact_ids"])
        subject_after = self.service.store.get_artifact(subject["artifact_id"])
        self.assertEqual(subject_digest, subject_after["dependency_digest"])
        self.assertEqual(subject_version, subject_after["version"])
        verification = self.service.verify()
        self.assertTrue(verification["derived_views"]["valid"], verification)

    def test_scenario_views_cover_subject_decision_and_workstream(self) -> None:
        self.seed_product()
        scenarios = self.service.store.list_artifacts(artifact_type="SCENARIO")
        kinds = {item["document"]["scenario_kind"] for item in scenarios}
        self.assertTrue(
            {"PROJECT", "SUBJECT", "DECISION_THREAD", "WORKSTREAM"}.issubset(kinds)
        )
        for item in scenarios:
            self.assertEqual(
                sorted(set(item["document"]["member_object_ids"])),
                item["document"]["member_object_ids"],
            )
            self.assertEqual(
                set(item["document"]["member_object_ids"]),
                set(item["provenance"]["member_dependency_digests"]),
            )

    def test_task_context_is_model_independent_and_explains_selection(self) -> None:
        self.seed_product()
        request = {
            "task": "Review the PostgreSQL migration decision",
            "purpose": "Continue Atlas",
            "query": "postgresql migration",
            "references": [],
            "depth": "EVIDENCE",
            "budget_bytes": 16 * 1024,
            "budget_tokens": None,
            "hints": {},
        }
        first = self.service.router.route(request)
        second = self.service.router.route(request)
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertEqual(first, second)
        self.assertGreater(len(first["selection_trace"]), 0)
        self.assertTrue(any(item["included"] for item in first["selection_trace"]))
        self.assertLessEqual(first["budget"]["included_bytes"], 16 * 1024)
        self.assertEqual(
            first["budget"]["included_bytes"],
            len(json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
        )
        with self.assertRaises(MemoryViewError) as partitioned:
            self.service.router.route(request | {"hints": {"model": "claude"}})
        self.assertEqual("AGENT_PARTITION_HINT_FORBIDDEN", partitioned.exception.code)

    def test_explicit_reference_is_preserved_or_fails_closed_on_budget(self) -> None:
        self.seed_product()
        decision = next(
            record for record in self.service.views.atomic_records() if record["kind"] == "DECISION_RECORD"
        )
        request = {
            "task": "Inspect exact decision",
            "purpose": None,
            "query": None,
            "references": [decision["object_id"]],
            "depth": "DETAIL",
            "budget_bytes": 8192,
            "budget_tokens": None,
            "hints": {},
        }
        response = self.service.router.route(request)
        included = {item["id"] for item in response["selection_trace"] if item["included"]}
        self.assertIn(decision["object_id"], included)
        with self.assertRaises(MemoryViewError):
            self.service.router.route(request | {"budget_bytes": 256})

    def test_open_conflict_is_exposed_in_core_and_scenario(self) -> None:
        first = self.write_source("a.md", "FACT: system:atlas | deployment.database_engine@1 | software:postgresql | production\n")
        second = self.write_source("b.md", "FACT: system:atlas | deployment.database_engine@1 | software:mysql | production\n")
        for source in (first, second):
            batch = self.service.ingest([source])
            self.service.extract(batch["batch_id"])
            self.service.commit_batch_drafts(batch["batch_id"])
        self.assertEqual(1, len(self.workspace.list_conflicts("OPEN")))
        self.service.build_memory_views()
        scenario = self.service.store.get_artifact("artifact_scenario-project")
        self.assertEqual(1, len(scenario["document"]["open_conflict_ids"]))
        incident = next(
            item
            for item in self.service.store.list_artifacts(artifact_type="SCENARIO")
            if item["document"]["scenario_kind"] == "INCIDENT"
        )
        self.assertEqual(scenario["document"]["open_conflict_ids"], incident["document"]["open_conflict_ids"])
        core = self.service.store.get_artifact("artifact_core-project")
        encoded = json.dumps(core["document"])
        conflict = self.workspace.list_conflicts("OPEN")[0]
        self.assertIn(conflict["conflict_id"], encoded)
        for member in conflict["members"]:
            self.assertIn(member, encoded)


if __name__ == "__main__":
    unittest.main()
