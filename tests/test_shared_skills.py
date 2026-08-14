from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from shared_mind.product import ProductError
from shared_mind.product_store import ProductStoreError
from shared_mind.skills import (
    SkillError,
    build_skill_record,
    create_skill,
    deprecate_skill,
    execute_skill,
    export_skill_package,
    import_skill_package,
    mark_skill_tested,
    revise_skill,
    select_skills,
)

from tests.product_support import ProductTestCase


class SharedSkillTest(ProductTestCase):
    def make_skill(self):
        return build_skill_record(
            skill_id="skill:migration-review",
            version=1,
            purpose="Review database migration plans",
            triggers=["migration review", "database cutover"],
            preconditions=["source material is available"],
            steps=["read sources", "check conflicts", "write review"],
            resources=[],
            expected_outputs=["review"],
            validation_rules=[{"type": "CONTAINS", "value": "review"}],
            provenance={"source": "test"},
        )

    def test_skill_lifecycle_requires_testing_before_approval(self) -> None:
        skill = create_skill(self.service.store, self.make_skill())
        with self.assertRaises(ProductError) as not_tested:
            self.service.approve_skill(skill["skill_id"], 1, approval={"by": "human:test"})
        self.assertEqual("SKILL_NOT_TESTED", not_tested.exception.code)
        tested = mark_skill_tested(
            self.service.store,
            skill["skill_id"],
            1,
            test_evidence={"passed": True},
        )
        self.assertEqual("TESTED", tested["status"])
        approved = self.service.approve_skill(
            skill["skill_id"], 1, approval={"by": "human:test"}
        )
        self.assertEqual("APPROVED", approved["status"])
        selected = select_skills(self.service.store, "review a database migration")
        self.assertEqual([skill["skill_id"]], [item["skill_id"] for item in selected])
        deprecated = deprecate_skill(
            self.service.store, skill["skill_id"], 1, rationale="replaced"
        )
        self.assertEqual("DEPRECATED", deprecated["status"])

    def test_tested_promotion_requires_explicit_passing_evidence(self) -> None:
        skill = create_skill(self.service.store, self.make_skill())
        with self.assertRaises(SkillError) as failed:
            mark_skill_tested(
                self.service.store,
                skill["skill_id"],
                1,
                test_evidence={"passed": False},
            )
        self.assertEqual("SKILL_TEST_EVIDENCE_FAILED", failed.exception.code)
        self.assertEqual(
            "DRAFT", self.service.store.get_skill(skill["skill_id"], version=1)["status"]
        )

    def test_revise_uses_optimistic_version_guard_and_one_shared_identity(self) -> None:
        create_skill(self.service.store, self.make_skill())
        revised = revise_skill(
            self.service.store,
            "skill:migration-review",
            expected_version=1,
            changes={"steps": ["read sources", "write stricter review"]},
        )
        self.assertEqual(2, revised["version"])
        self.assertEqual("skill:migration-review", revised["skill_id"])
        with self.assertRaises(SkillError) as stale:
            revise_skill(
                self.service.store,
                "skill:migration-review",
                expected_version=1,
                changes={"purpose": "stale"},
            )
        self.assertEqual("SKILL_VERSION_MISMATCH", stale.exception.code)
        self.assertEqual(2, len(self.service.store.list_skills()))


    def test_skill_mutations_use_idempotent_product_proposals_and_replay(self) -> None:
        skill = self.make_skill()
        first = create_skill(self.service.store, skill)
        repeated = create_skill(self.service.store, skill)
        self.assertEqual(first["content_hash"], repeated["content_hash"])
        self.assertEqual(1, len(self.service.store.list_product_proposals()))
        mark_skill_tested(
            self.service.store, skill["skill_id"], 1, test_evidence={"passed": True}
        )
        self.service.approve_skill(
            skill["skill_id"], 1, approval={"by": "human:test"}
        )
        replay = self.service.store.verify_skill_replay()
        self.assertTrue(replay["valid"])
        self.assertEqual(3, replay["proposal_count"])
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.store.connection.execute("DELETE FROM product_proposals")
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.store.connection.execute(
                "UPDATE product_receipts SET outcome='VALIDATION_ERROR'"
            )

    def test_stale_skill_product_proposal_is_auditable_transaction_conflict(self) -> None:
        create_skill(self.service.store, self.make_skill())
        revise_skill(
            self.service.store,
            "skill:migration-review",
            expected_version=1,
            changes={"steps": ["read sources", "write review"]},
        )
        replacement = build_skill_record(
            skill_id="skill:migration-review",
            version=2,
            purpose="Stale revision",
            triggers=["migration"],
            steps=["stale"],
            validation_rules=[{"type": "NON_EMPTY"}],
            provenance={"source": "stale-test"},
            created_at="2026-08-14T00:00:00Z",
        )
        proposal = {
            "object_type": "PRODUCT_MUTATION_PROPOSAL",
            "proposal_id": "product_proposal_" + "a" * 24,
            "idempotency_key": "product-skill:stale-revision",
            "proposer": "service:shared-mind-product",
            "proposed_at": "2026-08-14T00:00:00Z",
            "expected_product_state_hash": None,
            "operations": [
                {
                    "op_id": "product_op_" + "b" * 24,
                    "op": "REVISE_SKILL",
                    "skill_id": "skill:migration-review",
                    "expected_version": 1,
                    "replacement_skill": replacement,
                }
            ],
        }
        before = self.service.store.product_state_hash()
        receipt = self.service.store.commit_product_proposal(proposal)
        self.assertEqual("TRANSACTION_CONFLICT", receipt["outcome"])
        self.assertIn("SKILL_VERSION_MISMATCH", receipt["reason_codes"])
        self.assertEqual(before, self.service.store.product_state_hash())
        self.assertEqual(receipt, self.service.store.commit_product_proposal(proposal))

    def test_malformed_product_proposal_fails_closed_without_partial_state(self) -> None:
        before_state = self.service.store.product_state_hash()
        before_audit = self.service.store.verify_audit()
        with self.assertRaises(ProductStoreError) as invalid:
            self.service.store.commit_product_proposal(
                {
                    "object_type": "PRODUCT_MUTATION_PROPOSAL",
                    "operations": [],
                }
            )
        self.assertEqual("PRODUCT_PROPOSAL_INVALID", invalid.exception.code)
        self.assertEqual(before_state, self.service.store.product_state_hash())
        self.assertEqual(before_audit, self.service.store.verify_audit())
        self.assertEqual([], self.service.store.list_product_proposals())

    def test_execution_runs_steps_and_validation(self) -> None:
        skill = self.make_skill() | {"status": "TESTED"}
        result = execute_skill(
            skill,
            executor=lambda step, context: f"review:{context['step_index']}:{step}",
        )
        self.assertTrue(result["passed"])
        self.assertEqual(3, len(result["outputs"]))
        with self.assertRaises(SkillError):
            execute_skill(self.make_skill(), executor=lambda step, context: step)

    def test_portable_package_round_trip_and_tamper_detection(self) -> None:
        create_skill(self.service.store, self.make_skill())
        mark_skill_tested(
            self.service.store,
            "skill:migration-review",
            1,
            test_evidence={"passed": True},
        )
        self.service.approve_skill(
            "skill:migration-review", 1, approval={"by": "human:test"}
        )
        package = self.base / "migration.skill.zip"
        exported = export_skill_package(
            self.service.store, "skill:migration-review", package
        )
        self.assertTrue(package.is_file())
        other_root = self.base / "other"
        from shared_mind.workspace import Workspace
        from shared_mind.product import ProductService

        other = ProductService(Workspace.initialize(other_root, purpose="Other"))
        try:
            imported = import_skill_package(other.store, package)
            self.assertEqual(exported["manifest"]["skill_id"], imported["skill_id"])
            self.assertEqual(exported["manifest"]["skill_version"], imported["version"])
            self.assertEqual(self.service.store.get_skill("skill:migration-review", version=1)["content_hash"], imported["content_hash"])
        finally:
            other.close()
        tampered = self.base / "tampered.zip"
        with zipfile.ZipFile(package) as source_archive, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as target_archive:
            for name in source_archive.namelist():
                payload = source_archive.read(name)
                if name == "skill.json":
                    payload += b" "
                target_archive.writestr(name, payload)
        with self.assertRaises(SkillError) as mismatch:
            import_skill_package(self.service.store, tampered)
        self.assertEqual("SKILL_PACKAGE_HASH_MISMATCH", mismatch.exception.code)


if __name__ == "__main__":
    unittest.main()
