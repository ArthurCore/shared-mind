from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind import Kernel
from shared_mind.canonical import sha256_json


ROOT = Path(__file__).resolve().parents[1]


class KernelHardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text()
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text()
        )
        self.objects = {item["name"]: item["object"] for item in fixture_set["typed_objects"]}
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", registry)
        self.kernel.register_source(
            self.objects["source_revision_postgresql"],
            (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes(),
        )

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp.cleanup()

    def test_fr_011_runtime_schema_rejects_missing_idempotency_key(self) -> None:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        del proposal["idempotency_key"]

        receipt = self.kernel.commit(proposal)

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("SCHEMA_VALIDATION_FAILED",), receipt.reason_codes)
        self.assertEqual(1, self._count("ledger"))

    def test_fr_011_runtime_schema_rejects_unknown_guard(self) -> None:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        proposal["guards"] = [{"op": "BOGUS_GUARD"}]

        receipt = self.kernel.commit(proposal)

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("SCHEMA_VALIDATION_FAILED",), receipt.reason_codes)
        self.assertEqual(1, self._count("ledger"))

    def test_fr_011_non_object_proposal_returns_a_structured_error(self) -> None:
        receipt = self.kernel.commit([])  # type: ignore[arg-type]

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("SCHEMA_VALIDATION_FAILED",), receipt.reason_codes)
        self.assertFalse(self.kernel.connection.in_transaction)
        self.assertEqual(1, self._count("ledger"))

    def test_fr_011_non_json_value_returns_a_structured_error(self) -> None:
        receipt = self.kernel.commit({"unsupported": {"set"}})

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("MALFORMED_PROPOSAL",), receipt.reason_codes)
        self.assertFalse(self.kernel.connection.in_transaction)
        self.assertEqual(1, self._count("ledger"))
        persisted = self.kernel.connection.execute(
            """SELECT outcome, reason_codes, ledger_seq
               FROM receipts
               WHERE reason_codes = '["MALFORMED_PROPOSAL"]'
               ORDER BY id"""
        ).fetchall()
        self.assertEqual(1, len(persisted))
        self.assertEqual("VALIDATION_ERROR", persisted[0]["outcome"])
        self.assertEqual('["MALFORMED_PROPOSAL"]', persisted[0]["reason_codes"])
        self.assertIsNone(persisted[0]["ledger_seq"])

    def test_fr_011_duplicate_ids_are_normalized_and_rolled_back(self) -> None:
        first = self.kernel.commit(self.objects["assert_postgresql_proposal"])
        self.assertEqual("COMMITTED", first.outcome)
        root_before = self.kernel.state_root()
        duplicate = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        duplicate["proposal_id"] = "proposal_duplicate_claim_ids"
        duplicate["idempotency_key"] = "duplicate-claim-ids-001"

        receipt = self.kernel.commit(duplicate)

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("DUPLICATE_OBJECT_ID",), receipt.reason_codes)
        self.assertFalse(self.kernel.connection.in_transaction)
        self.assertEqual(2, self._count("ledger"))
        self.assertEqual(1, self._count("claims"))
        self.assertEqual(root_before, self.kernel.state_root())

    def test_fr_015_rejects_every_unsupported_pinned_version(self) -> None:
        cases = {
            "schema": ("99.0.0", "UNSUPPORTED_SCHEMA_VERSION"),
            "predicate_registry": ("99.0.0", "UNSUPPORTED_PREDICATE_REGISTRY"),
            "conflict_rules": ("conflict-rules@9", "UNSUPPORTED_CONFLICT_RULES_VERSION"),
            "guard_dsl": ("guard-dsl@9", "UNSUPPORTED_GUARD_DSL_VERSION"),
            "projection": ("markdown-projection@9", "UNSUPPORTED_PROJECTION_VERSION"),
        }
        for index, (field, (value, expected_code)) in enumerate(cases.items(), start=1):
            with self.subTest(field=field):
                proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
                proposal["proposal_id"] = f"proposal_bad_version_{index:02d}"
                proposal["idempotency_key"] = f"bad-version-{field}-001"
                proposal["versions"][field] = value
                operation = proposal["operations"][0]
                claim_id = f"claim_bad_version_{index:02d}"
                operation["claim"]["claim_id"] = claim_id
                operation["initial_evidence"][0]["claim_id"] = claim_id
                operation["initial_evidence"][0][
                    "evidence_link_id"
                ] = f"evidence_bad_version_{index:02d}"

                receipt = self.kernel.commit(proposal)

                self.assertEqual("VALIDATION_ERROR", receipt.outcome)
                self.assertEqual((expected_code,), receipt.reason_codes)
        self.assertEqual(1, self._count("ledger"))

    def test_fr_011_registry_semantics_are_enforced_at_runtime(self) -> None:
        cases = (
            (
                "object_kind",
                lambda proposition, evidence: proposition.update(
                    {"object": {"kind": "enum", "value": "ACTIVE"}}
                ),
                "OBJECT_KIND_MISMATCH",
            ),
            (
                "object_entity_type",
                lambda proposition, evidence: proposition["object"].update(
                    {"entity_type": "team"}
                ),
                "OBJECT_ENTITY_TYPE_MISMATCH",
            ),
            (
                "scope_allowed_fields",
                lambda proposition, evidence: (
                    proposition.update(
                        {
                            "predicate": "deployment.region@1",
                            "object": {
                                "kind": "entity",
                                "entity_type": "region",
                                "entity_id": "region:ap-northeast-2",
                            },
                        }
                    ),
                    proposition["scope"].update({"region": "ap-northeast-2"}),
                ),
                "SCOPE_FIELD_NOT_ALLOWED",
            ),
            (
                "temporal_required",
                lambda proposition, evidence: proposition.update(
                    {"valid_time": {"from": None, "to": None}}
                ),
                "VALID_TIME_REQUIRED",
            ),
            (
                "temporal_range",
                lambda proposition, evidence: proposition.update(
                    {
                        "valid_time": {
                            "from": "2026-09-01T00:00:00Z",
                            "to": "2026-08-01T00:00:00Z",
                        }
                    }
                ),
                "INVALID_VALID_TIME_RANGE",
            ),
            (
                "evidence_interpretation",
                lambda proposition, evidence: (
                    proposition.update(
                        {"predicate": "migration.target_database_engine@1"}
                    ),
                    evidence.update({"interpretation": "INFERRED"}),
                ),
                "EVIDENCE_INTERPRETATION_NOT_ALLOWED",
            ),
        )

        for index, (name, mutate, expected_code) in enumerate(cases, start=1):
            with self.subTest(rule=name):
                proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
                proposal["proposal_id"] = f"proposal_registry_rule_{index:02d}"
                proposal["idempotency_key"] = f"registry-rule-{index:02d}"
                operation = proposal["operations"][0]
                claim = operation["claim"]
                evidence = operation["initial_evidence"][0]
                claim_id = f"claim_registry_rule_{index:02d}"
                claim["claim_id"] = claim_id
                evidence["claim_id"] = claim_id
                evidence["evidence_link_id"] = f"evidence_registry_rule_{index:02d}"
                mutate(claim["proposition"], evidence)
                claim["proposition_hash"] = sha256_json(claim["proposition"])

                receipt = self.kernel.commit(proposal)

                self.assertEqual("VALIDATION_ERROR", receipt.outcome)
                self.assertEqual((expected_code,), receipt.reason_codes)

        self.assertEqual(1, self._count("ledger"))

    def test_fr_024_destructive_operation_requires_a_claim_version_read(self) -> None:
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.objects["assert_postgresql_proposal"]).outcome,
        )
        attach = self._attach_extra_evidence_proposal()
        self.assertEqual("COMMITTED", self.kernel.commit(attach).outcome)
        ledger_before = self._count("ledger")
        stale = copy.deepcopy(self.objects["stale_supersede_proposal"])
        stale["proposal_id"] = "proposal_stale_without_read"
        stale["idempotency_key"] = "stale-without-read-001"
        stale["reads"] = []
        stale["guards"] = [
            {
                "op": "CLAIM_STATUS_EQ",
                "claim_id": "claim_atlas_postgresql_001",
                "expected_status": "ACTIVE",
            }
        ]

        receipt = self.kernel.commit(stale)

        self.assertEqual("VALIDATION_ERROR", receipt.outcome)
        self.assertEqual(("MISSING_REQUIRED_CLAIM_READ",), receipt.reason_codes)
        self.assertEqual(ledger_before, self._count("ledger"))
        target = self.kernel.connection.execute(
            "SELECT status, version FROM claims WHERE claim_id = ?",
            ("claim_atlas_postgresql_001",),
        ).fetchone()
        self.assertEqual(("ACTIVE", 2), tuple(target))
        self.assertEqual(0, self.kernel.connection.execute(
            "SELECT COUNT(*) FROM claims WHERE claim_id = ?",
            ("claim_atlas_postgresql_002",),
        ).fetchone()[0])

    def test_fr_022_supersede_does_not_conflict_with_its_inactive_target(self) -> None:
        self.assertEqual(
            "COMMITTED",
            self.kernel.commit(self.objects["assert_postgresql_proposal"]).outcome,
        )
        proposal = copy.deepcopy(self.objects["stale_supersede_proposal"])
        proposal["proposal_id"] = "proposal_supersede_with_mysql"
        proposal["idempotency_key"] = "supersede-with-mysql-001"
        mysql_assert = self.objects["assert_mysql_same_interval_proposal"]["operations"][0]
        replacement = copy.deepcopy(mysql_assert["claim"])
        replacement["claim_id"] = "claim_atlas_mysql_replacement"
        evidence = copy.deepcopy(mysql_assert["initial_evidence"])
        evidence[0]["claim_id"] = replacement["claim_id"]
        evidence[0]["evidence_link_id"] = "evidence_atlas_mysql_replace"
        proposal["operations"][0]["replacement_claim"] = replacement
        proposal["operations"][0]["initial_evidence"] = evidence

        receipt = self.kernel.commit(proposal)

        self.assertEqual("COMMITTED", receipt.outcome)
        self.assertEqual((), receipt.conflict_ids)
        self.assertEqual(0, self._count("conflicts"))
        statuses = {
            row["claim_id"]: row["status"]
            for row in self.kernel.connection.execute(
                "SELECT claim_id, status FROM claims ORDER BY claim_id"
            )
        }
        self.assertEqual("SUPERSEDED", statuses["claim_atlas_postgresql_001"])
        self.assertEqual("ACTIVE", statuses["claim_atlas_mysql_replacement"])

    def _attach_extra_evidence_proposal(self) -> dict[str, object]:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        link = copy.deepcopy(proposal["operations"][0]["initial_evidence"][0])
        link["evidence_link_id"] = "evidence_atlas_postgresql_extra"
        proposal["proposal_id"] = "proposal_attach_extra_evidence"
        proposal["idempotency_key"] = "atlas-attach-extra-001"
        proposal["operations"] = [
            {
                "op_id": "operation_attach_extra",
                "op": "ATTACH_EVIDENCE",
                "evidence_link": link,
            }
        ]
        return proposal

    def _count(self, table: str) -> int:
        return int(self.kernel.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
