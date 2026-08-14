from __future__ import annotations

import json
import sqlite3
import stat
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared_mind.product import ProductError, ProductService
from shared_mind.skills import build_skill_record, create_skill, mark_skill_tested

from tests.product_support import ProductTestCase


class ProductGovernanceEvalTest(ProductTestCase):
    def test_catalog_review_queue_and_provenance(self) -> None:
        source = self.write_source()
        batch = self.service.ingest([source])
        extraction = self.service.extract(batch["batch_id"])
        queue = self.service.review_queue()
        self.assertEqual(2, len(queue["drafts"]))
        self.assertTrue(all(item["provenance"] for item in queue["drafts"]))
        self.service.commit_batch_drafts(batch["batch_id"])
        self.service.build_memory_views()
        catalog = self.service.catalog()
        kinds = {item["kind"] for item in catalog["items"]}
        self.assertIn("CLAIM", kinds)
        self.assertIn("SCENARIO", kinds)
        self.assertIn("SKILL", kinds)
        self.assertEqual(0, len(self.service.review_queue()["drafts"]))
        self.assertEqual(set(extraction["draft_ids"]), {item["draft_id"] for item in self.service.list_drafts()})

    def test_product_audit_is_append_only_and_tamper_evident(self) -> None:
        self.seed_product()
        audit = self.service.store.verify_audit()
        self.assertTrue(audit["valid"])
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.store.connection.execute("DELETE FROM product_audit")
        external = sqlite3.connect(self.service.store.path)
        try:
            external.execute("DROP TRIGGER product_audit_no_update")
            external.execute("UPDATE product_audit SET event_type='TAMPERED' WHERE seq=1")
            external.commit()
        finally:
            external.close()
        self.assertFalse(self.service.store.verify_audit()["valid"])
        self.assertFalse(self.service.verify()["valid"])

    def test_backup_restore_preserves_kernel_and_product_hashes(self) -> None:
        self.seed_product()
        package = self.base / "backup.zip"
        exported = self.service.export_backup(package)
        restored_root = self.base / "restored"
        restored = ProductService.restore_backup(package, restored_root)
        self.assertEqual(
            exported["manifest"]["kernel_state_root"],
            restored["verify"]["kernel_state_root"],
        )
        self.assertEqual(
            exported["manifest"]["product_state_hash"],
            restored["verify"]["product_state_hash"],
        )
        tampered = self.base / "tampered.zip"
        with zipfile.ZipFile(package) as original, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as rewritten:
            manifest = json.loads(original.read("manifest.json"))
            target = manifest["files"][0]["path"]
            for name in original.namelist():
                payload = original.read(name)
                if name == target:
                    payload += b"tampered"
                rewritten.writestr(name, payload)
        with self.assertRaises(ProductError) as caught:
            failed_destination = self.base / "tampered-restored"
            ProductService.restore_backup(tampered, failed_destination)
        self.assertEqual("BACKUP_FILE_HASH_MISMATCH", caught.exception.code)
        self.assertFalse(failed_destination.exists())

    def test_restore_rejects_duplicate_unexpected_symlink_and_oversized_entries(self) -> None:
        self.seed_product()
        package = self.base / "backup.zip"
        self.service.export_backup(package)

        duplicate = self.base / "duplicate.zip"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(duplicate, "w") as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.writestr("manifest.json", source.read("manifest.json"))
        with self.assertRaises(ProductError) as duplicate_error:
            ProductService.restore_backup(duplicate, self.base / "duplicate-restored")
        self.assertEqual("BACKUP_DUPLICATE_ENTRY", duplicate_error.exception.code)

        unexpected = self.base / "unexpected.zip"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(unexpected, "w") as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.writestr("not-declared.txt", b"unexpected")
        with self.assertRaises(ProductError) as unexpected_error:
            ProductService.restore_backup(unexpected, self.base / "unexpected-restored")
        self.assertEqual("BACKUP_UNEXPECTED_ENTRY", unexpected_error.exception.code)

        symlink = self.base / "symlink.zip"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(symlink, "w") as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            target.writestr(link, b"manifest.json")
        with self.assertRaises(ProductError) as symlink_error:
            ProductService.restore_backup(symlink, self.base / "symlink-restored")
        self.assertEqual("BACKUP_SYMLINK_DENIED", symlink_error.exception.code)

        with patch("shared_mind.product.BACKUP_MAX_MEMBER_BYTES", 8):
            with self.assertRaises(ProductError) as size_error:
                ProductService.restore_backup(package, self.base / "oversized-restored")
        self.assertEqual("BACKUP_MEMBER_TOO_LARGE", size_error.exception.code)

    def test_post_task_capture_and_incremental_compounding(self) -> None:
        result = self.service.post_task_capture(
            "task-001",
            "WORK: P1 | Record deployment rollback verification\n",
            auto_commit_deterministic=True,
        )
        self.assertEqual(1, result["extraction"]["created"])
        self.assertEqual(1, len(result["commit"]["committed"]))
        work = [
            record
            for record in self.service.views.atomic_records()
            if record["kind"] == "WORK_ITEM"
        ]
        self.assertEqual(1, len(work))
        self.assertIn("artifact_core-project", result["consolidation"]["views"]["artifacts"])

    def test_memory_routing_skill_and_cold_start_metrics(self) -> None:
        self.seed_product()
        quality = self.service.memory_quality_metrics()
        self.assertEqual(1.0, quality["evidence_validity"])
        self.assertEqual(1.0, quality["provenance_completeness"])
        claim_id = next(
            record["object_id"]
            for record in self.service.views.atomic_records()
            if record["kind"] == "CLAIM"
        )
        request = {
            "task": "Review PostgreSQL migration",
            "purpose": None,
            "query": "postgresql",
            "references": [claim_id],
            "depth": "DETAIL",
            "budget_bytes": 16 * 1024,
            "budget_tokens": None,
            "hints": {},
        }
        routing = self.service.context_routing_metrics(
            request, expected_ids=[claim_id], repetitions=3
        )
        self.assertTrue(routing["cross_client_parity"])
        self.assertTrue(routing["core_context_parity"])
        self.assertTrue(routing["core_context_preserved"])
        self.assertEqual(1.0, routing["relevant_recall"])
        self.assertGreaterEqual(routing["irrelevant_context_rate"], 0.0)

        skill = build_skill_record(
            skill_id="skill:fixture-review",
            version=1,
            purpose="Review migration",
            triggers=["migration"],
            steps=["write review"],
            validation_rules=[{"type": "NON_EMPTY"}],
            provenance={"test": True},
        )
        create_skill(self.service.store, skill)
        mark_skill_tested(
            self.service.store,
            skill["skill_id"],
            1,
            test_evidence={"passed": True},
        )
        benchmark = self.service.skill_reuse_benchmark(
            skill["skill_id"],
            1,
            executor=lambda step, context: "review complete",
            baseline=lambda: {"passed": False, "turns": 4},
        )
        self.assertTrue(benchmark["success_improved"])
        self.assertGreater(benchmark["turn_reduction"], 0)

        handoff = self.service.context(request)
        cold = self.service.cold_start_benchmark(
            handoff,
            manual_explanation="manual " * 10000,
            expected_ids=[claim_id],
        )
        self.assertGreater(cold["byte_reduction"], 0.5)
        self.assertEqual(1.0, cold["expected_recall"])

    def test_usage_telemetry_is_minimal_and_covers_context_search_tool_and_skill(self) -> None:
        self.seed_product()
        scenario = self.service.store.get_artifact("artifact_scenario-project")
        self.service.context(
            {
                "task": "Review PostgreSQL migration",
                "purpose": None,
                "query": "postgresql",
                "references": [],
                "depth": "DETAIL",
                "budget_bytes": 16 * 1024,
                "budget_tokens": None,
                "hints": {},
            }
        )
        self.service.search("postgresql")
        self.service.tool_call("get_artifact", {"artifact_id": scenario["artifact_id"]})

        skill = build_skill_record(
            skill_id="skill:telemetry-review",
            version=1,
            purpose="Review migration telemetry",
            triggers=["migration"],
            steps=["write review"],
            validation_rules=[{"type": "NON_EMPTY"}],
            provenance={"test": True},
        )
        create_skill(self.service.store, skill)
        self.service.skill_reuse_benchmark(
            skill["skill_id"],
            1,
            executor=lambda step, context: "review complete",
            baseline=lambda: {"passed": False, "turns": 2},
        )
        events = self.service.store.list_telemetry()
        event_types = {item["event_type"] for item in events}
        self.assertTrue(
            {
                "CONTEXT_ROUTED",
                "MEMORY_SEARCHED",
                "PRODUCT_TOOL_CALLED",
                "SKILL_REUSE_EVALUATED",
            }.issubset(event_types)
        )
        encoded = json.dumps(events).casefold()
        self.assertNotIn("review postgresql migration", encoded)
        self.assertNotIn('"query": "postgresql"', encoded)

    def test_single_command_cold_start_is_incremental(self) -> None:
        self.write_source()
        first = self.service.cold_start(
            [self.workspace_root],
            task="Continue Atlas implementation",
            budget_bytes=16 * 1024,
        )
        self.assertEqual(1, first["ingest"]["imported"])
        self.assertGreater(first["committed"], 0)
        self.assertIn("context_hash", first["first_handoff"])
        self.assertIn("handoff_hash", first["first_handoff"])
        self.assertGreater(len(first["first_handoff"]["source_map"]), 0)
        self.assertGreater(
            len(first["first_handoff"]["recommended_next_actions"]), 0
        )
        self.assertEqual(
            "WORK_ITEM",
            first["first_handoff"]["recommended_next_actions"][0]["kind"],
        )
        second = self.service.cold_start(
            [self.workspace_root],
            task="Continue Atlas implementation",
            budget_bytes=16 * 1024,
        )
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(0, second["extraction"]["created"])
        self.assertEqual(first["first_handoff"]["kernel_state_root"], second["first_handoff"]["kernel_state_root"])
        self.assertEqual(
            first["first_handoff"]["handoff_hash"],
            second["first_handoff"]["handoff_hash"],
        )

    def test_product_verify_rebuilds_managed_views_and_detects_sql_tampering(self) -> None:
        self.seed_product()
        clean = self.service.verify()
        self.assertTrue(clean["derived_views"]["valid"])
        external = sqlite3.connect(self.service.store.path)
        try:
            external.execute(
                "UPDATE artifacts SET document=? WHERE artifact_id=?",
                ('{"tampered":true}', "artifact_scenario-project"),
            )
            external.commit()
        finally:
            external.close()
        report = self.service.verify()
        self.assertFalse(report["valid"])
        self.assertFalse(report["derived_views"]["valid"])
        self.assertIn(
            "artifact_scenario-project", report["derived_views"]["mismatched"]
        )

    def test_product_verify_detects_missing_artifact_provenance(self) -> None:
        self.seed_product()
        artifact = self.service.store.get_artifact("artifact_scenario-project")
        artifact["artifact_id"] = "artifact_fixture-missing-provenance"
        artifact["dependency_digest"] = "sha256:" + "e" * 64
        artifact["provenance"] = {}
        with self.service.store.transaction():
            self.service.store.put_artifact(artifact)
        report = self.service.verify()
        self.assertFalse(report["valid"])
        self.assertIn("artifact_fixture-missing-provenance", report["artifact_provenance_issues"])


if __name__ == "__main__":
    unittest.main()
