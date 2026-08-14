from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from shared_mind.product import ProductError
from shared_mind.product_ingest import ExtractionLimits, ProductIngestError

from tests.product_support import DIRECTIVES, ProductTestCase


class _InvalidModelExtractor:
    extractor_id = "fixture-model"
    extractor_version = "1"
    model = "fixture/model"
    prompt_version = "fixture-prompt@1"

    def extract(self, *, source_revision, content, limits):
        del content, limits
        return {
            "operations": [
                {
                    "op_id": "operation_invalid_model_001",
                    "op": "ASSERT_CLAIM",
                    "claim": {
                        "object_type": "CLAIM",
                        "claim_id": "claim_invalid_model_001",
                        "proposition_hash": "sha256:" + "0" * 64,
                        "proposition": {
                            "proposition_version": 1,
                            "subject": {"entity_id": "system:atlas", "entity_type": "system"},
                            "predicate": "deployment.database_engine@1",
                            "object": {
                                "kind": "entity",
                                "entity_id": "software:postgresql",
                                "entity_type": "software",
                            },
                            "polarity": "POSITIVE",
                            "scope": {
                                "component": None,
                                "environment": "production",
                                "region": None,
                                "tenant": None,
                            },
                            "valid_time": {"from": source_revision["captured_at"], "to": None},
                        },
                        "asserted_by": {"actor_id": "service:model", "actor_type": "SERVICE"},
                        "asserted_at": source_revision["captured_at"],
                    },
                    "initial_evidence": [],
                }
            ],
            "skills": [],
        }


class _SlowModelExtractor:
    extractor_id = "fixture-slow-model"
    extractor_version = "1"
    model = "fixture/slow"
    prompt_version = "fixture-prompt@1"

    def extract(self, *, source_revision, content, limits):
        del source_revision, content, limits
        time.sleep(0.05)
        return {"operations": [], "skills": []}


class ProductIngestTest(ProductTestCase):
    def test_end_to_end_extraction_has_exact_evidence_and_review_boundary(self) -> None:
        source = self.write_source()
        batch = self.service.ingest([source])
        self.assertEqual({"total": 1, "imported": 1, "unchanged": 0, "failed": 0, "skipped": 0}, batch["summary"])
        extraction = self.service.extract(batch["batch_id"])
        self.assertEqual("COMPLETED", extraction["status"])
        self.assertEqual(2, extraction["created"])
        self.assertEqual(0, self.workspace.list_conflicts("OPEN").__len__())
        drafts = self.service.list_drafts(batch_id=batch["batch_id"])
        kernel_draft = next(item for item in drafts if item["draft_kind"] == "KERNEL_PROPOSAL")
        fact = next(
            operation
            for operation in kernel_draft["document"]["operations"]
            if operation["op"] == "ASSERT_CLAIM"
        )
        evidence = fact["initial_evidence"][0]
        source_bytes = source.read_bytes()
        selector = evidence["selector"]
        selected = source_bytes[selector["start_byte"] : selector["end_byte"]]
        self.assertEqual(selector["excerpt"], selected.decode("utf-8"))
        self.assertEqual("DRAFT", kernel_draft["status"])
        result = self.service.commit_batch_drafts(batch["batch_id"])
        self.assertEqual(2, len(result["committed"]))
        self.assertEqual([], result["failed"])
        self.assertGreater(len(self.service.views.atomic_records()), 4)

    def test_unchanged_reimport_and_reextract_are_idempotent(self) -> None:
        source = self.write_source()
        first = self.service.ingest([source])
        first_extract = self.service.extract(first["batch_id"])
        second = self.service.ingest([source])
        second_extract = self.service.extract(second["batch_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(0, second_extract["created"])
        self.assertEqual(first_extract["created"], second_extract["duplicates"])
        kernel = self.workspace.open_kernel()
        try:
            source_count = kernel.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        finally:
            kernel.close()
        self.assertEqual(1, source_count)

    def test_conversation_timestamp_is_preserved(self) -> None:
        conversation = self.write_source(
            "session.jsonl",
            json.dumps(
                {
                    "timestamp": "2026-01-02T03:04:05Z",
                    "content": "WORK: P1 | Continue the migration review",
                }
            )
            + "\n",
        )
        batch = self.service.ingest([], conversation_paths=[conversation])
        self.assertEqual("2026-01-02T03:04:05Z", batch["items"][0]["captured_at"])
        extraction = self.service.extract(batch["batch_id"])
        draft = self.service.get_draft(extraction["draft_ids"][0])
        operation = draft["document"]["operations"][0]
        self.assertEqual("2026-01-02T03:04:05Z", operation["work_item"]["created_at"])

    def test_policy_and_resource_limits_fail_closed(self) -> None:
        source = self.write_source()
        batch = self.service.ingest([source])
        with self.assertRaises(ProductError) as denied:
            self.service.extract(batch["batch_id"], model_extractor=_InvalidModelExtractor())
        self.assertEqual("REMOTE_DISCLOSURE_NOT_AUTHORIZED", denied.exception.code)
        with self.assertRaises(ProductIngestError) as too_large:
            self.service.ingest_manager.ingest([source], max_file_bytes=8)
        self.assertEqual("INGEST_FILE_TOO_LARGE", too_large.exception.code)
        limited = self.service.ingest_manager.extract(
            batch["batch_id"], limits=ExtractionLimits(max_characters=8)
        )
        self.assertEqual("FAILED", limited["status"])
        self.assertEqual("EXTRACTION_SOURCE_TOO_LARGE", limited["failures"][0]["code"])
        token_limited = self.service.ingest_manager.extract(
            batch["batch_id"], limits=ExtractionLimits(max_tokens=1)
        )
        self.assertEqual("EXTRACTION_TOKEN_LIMIT", token_limited["failures"][0]["code"])
        timed_out = self.service.ingest_manager.extract(
            batch["batch_id"],
            model_extractor=_SlowModelExtractor(),
            remote_policy_decision={"outcome": "ALLOW", "reason_codes": []},
            limits=ExtractionLimits(timeout_seconds=0.01),
        )
        self.assertEqual("EXTRACTION_TIMEOUT", timed_out["failures"][0]["code"])

    def test_model_invalid_candidate_stays_staged_and_cannot_mutate_kernel(self) -> None:
        source = self.write_source(content="This source has no directives.\n")
        batch = self.service.ingest([source])
        extraction = self.service.extract(
            batch["batch_id"],
            model_extractor=_InvalidModelExtractor(),
            remote_policy_decision={"outcome": "ALLOW", "reason_codes": []},
        )
        self.assertEqual(1, extraction["created"])
        draft = self.service.get_draft(extraction["draft_ids"][0])
        self.assertEqual("MODEL_BACKED", draft["provenance"]["mode"])
        with self.assertRaises(ProductError) as failed:
            self.service.commit_draft(draft["draft_id"])
        self.assertEqual("DRAFT_COMMIT_FAILED", failed.exception.code)
        kernel = self.workspace.open_kernel()
        try:
            self.assertEqual(0, kernel.connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
        finally:
            kernel.close()

    def test_partial_registration_failure_is_reported_without_losing_success(self) -> None:
        first = self.write_source("a.md", "WORK: P1 | Keep first\n")
        second = self.write_source("b.md", "WORK: P1 | Keep second\n")
        original = self.service.ingest_manager._register_source

        def flaky(*, content, source_id, **kwargs):
            if source_id.startswith("document:b.md"):
                raise ProductIngestError("FIXTURE_FAILURE", "forced")
            return original(content=content, source_id=source_id, **kwargs)

        with mock.patch.object(self.service.ingest_manager, "_register_source", side_effect=flaky):
            batch = self.service.ingest_manager.ingest([first, second])
        self.assertEqual("PARTIAL", batch["status"])
        self.assertEqual(1, batch["summary"]["imported"])
        self.assertEqual(1, batch["summary"]["failed"])
        resumed = self.service.ingest_manager.ingest([first, second])
        self.assertTrue(resumed["resumed"])
        self.assertEqual("COMPLETED", resumed["status"])
        self.assertEqual(2, resumed["summary"]["imported"])
        self.assertEqual(0, resumed["summary"]["failed"])
        kernel = self.workspace.open_kernel()
        try:
            self.assertEqual(
                2, kernel.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            )
        finally:
            kernel.close()

    def test_bulk_ingest_excludes_only_the_workspace_projection_root(self) -> None:
        generated = self.workspace.projection_root / "project.md"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("WORK: P0 | Do not ingest generated view\n", encoding="utf-8")
        legitimate = self.workspace_root / "docs" / "projections" / "source.md"
        legitimate.parent.mkdir(parents=True, exist_ok=True)
        legitimate.write_text("WORK: P1 | Keep legitimate source\n", encoding="utf-8")

        batch = self.service.ingest([self.workspace_root])
        paths = {Path(item["source_path"]).resolve() for item in batch["items"]}
        self.assertNotIn(generated.resolve(), paths)
        self.assertIn(legitimate.resolve(), paths)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_symlink_root_is_denied(self) -> None:
        source = self.write_source()
        link = self.base / "link.md"
        link.symlink_to(source)
        with self.assertRaises(ProductError) as caught:
            self.service.ingest([link])
        self.assertEqual("INGEST_SYMLINK_DENIED", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
