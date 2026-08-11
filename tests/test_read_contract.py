from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
READ_SCHEMA = ROOT / "contracts" / "shared-mind-read.schema.v1.json"

QUERY_KINDS = (
    "SOURCE_REVISION",
    "CLAIM",
    "EVIDENCE_LINK",
    "CONFLICT",
    "DECISION_RECORD",
    "OPEN_QUESTION",
    "WORK_ITEM",
)

UNFILTERED_QUERY: dict[str, Any] = {
    "query_version": "structured-query@1",
    "kinds": [],
    "ids": [],
    "title_contains": None,
    "predicates": [],
    "source_ids": [],
    "source_revision_ids": [],
    "statuses": [],
    "limit": 100,
    "offset": 0,
    "include_record": True,
}

FILTERED_QUERY: dict[str, Any] = {
    "query_version": "structured-query@1",
    "kinds": ["CLAIM", "DECISION_RECORD"],
    "ids": [
        "claim_atlas_postgresql_001",
        "decision_atlas_database_strategy_001",
    ],
    "title_contains": "production database",
    "predicates": ["deployment.database_engine@1"],
    "source_ids": ["document:atlas-runbook"],
    "source_revision_ids": ["revision_atlas_runbook_20260801"],
    "statuses": ["ACTIVE"],
    "limit": 1,
    "offset": 0,
    "include_record": False,
}

QUERY_RESULT: dict[str, Any] = {
    "query_version": "structured-query@1",
    "projection_version": "markdown-projection@3",
    "state_root": "sha256:" + "1" * 64,
    "ledger_sequence": 7,
    "normalized_query": FILTERED_QUERY,
    "hits": [
        {
            "object_type": "CLAIM",
            "object_id": "claim_atlas_postgresql_001",
            "projection_ref": "project.json#/claims/0",
            "matched_fields": ["id", "predicate", "source_revision_id"],
            "summary": "Atlas uses PostgreSQL in production.",
            "record": None,
        }
    ],
    "total_matches": 2,
    "truncated": True,
}

REBASE_HINT: dict[str, Any] = {
    "hint_version": "rebase-hint@1",
    "advisory": True,
    "proposal_id": "proposal_stale_supersede_01",
    "receipt_id": "receipt_stale_supersede_01",
    "reason_code": "CLAIM_VERSION_MISMATCH",
    "observed_state_root": "sha256:" + "2" * 64,
    "observed_ledger_head": "sha256:" + "3" * 64,
    "failed_precondition": {
        "path": "$.reads[0].expected_version",
        "expected": 1,
        "actual": 2,
        "aggregate_type": "CLAIM",
        "aggregate_id": "claim_atlas_postgresql_001",
        "actual_state": {"version": 2, "status": "SUPERSEDED"},
    },
    "replacement_preconditions": [
        {"path": "$.reads[0].expected_version", "value": 2},
        {"path": "$.guards[0].expected_status", "value": "SUPERSEDED"},
        {"path": "$.guards[1].expected_version", "value": 2},
    ],
    "safe_to_auto_apply": False,
    "recommended_action": "REVIEW_AND_REBUILD",
}

POSITIVE_FIXTURES = {
    "unfiltered_structured_query": UNFILTERED_QUERY,
    "filtered_structured_query": FILTERED_QUERY,
    "query_result_summary_only": QUERY_RESULT,
    "transaction_conflict_rebase_hint": REBASE_HINT,
}


class ReadContractPresenceTest(unittest.TestCase):
    def test_dev_020_read_contract_schema_exists(self) -> None:
        self.assertTrue(
            READ_SCHEMA.is_file(),
            "DEV-020 requires contracts/shared-mind-read.schema.v1.json",
        )


@unittest.skipUnless(
    READ_SCHEMA.is_file(),
    "read contract schema has not been implemented yet (expected RED)",
)
class ReadContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(READ_SCHEMA.read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(
            cls.schema,
            format_checker=FormatChecker(),
        )

    def test_schema_is_draft_2020_12_and_exposes_three_versioned_documents(
        self,
    ) -> None:
        Draft202012Validator.check_schema(self.schema)
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema",
            self.schema["$schema"],
        )
        for definition in ("StructuredQuery", "QueryResult", "RebaseHint"):
            with self.subTest(definition=definition):
                self.assertIn(definition, self.schema["$defs"])
                self.assertIs(
                    False,
                    self.schema["$defs"][definition]["additionalProperties"],
                )

        self.assertEqual(
            {
                "#/$defs/StructuredQuery",
                "#/$defs/QueryResult",
                "#/$defs/RebaseHint",
            },
            {branch["$ref"] for branch in self.schema["oneOf"]},
        )

    def test_structured_query_has_only_the_supported_filters_and_pinned_limits(
        self,
    ) -> None:
        definition = self.schema["$defs"]["StructuredQuery"]
        expected_fields = {
            "query_version",
            "kinds",
            "ids",
            "title_contains",
            "predicates",
            "source_ids",
            "source_revision_ids",
            "statuses",
            "limit",
            "offset",
            "include_record",
        }
        self.assertEqual(expected_fields, set(definition["properties"]))
        self.assertEqual(expected_fields, set(definition["required"]))
        self.assertEqual(
            "structured-query@1",
            definition["properties"]["query_version"]["const"],
        )
        self.assertEqual(
            list(QUERY_KINDS),
            definition["properties"]["kinds"]["items"]["enum"],
        )
        self.assertEqual(1, definition["properties"]["limit"]["minimum"])
        self.assertEqual(1000, definition["properties"]["limit"]["maximum"])
        self.assertEqual(0, definition["properties"]["offset"]["minimum"])

    def test_query_result_is_the_stable_read_output_envelope(self) -> None:
        definition = self.schema["$defs"]["QueryResult"]
        expected_fields = {
            "query_version",
            "projection_version",
            "state_root",
            "ledger_sequence",
            "normalized_query",
            "hits",
            "total_matches",
            "truncated",
        }
        self.assertEqual(expected_fields, set(definition["properties"]))
        self.assertEqual(expected_fields, set(definition["required"]))
        self.assertNotIn("ok", definition["properties"])
        self.assertNotIn("code", definition["properties"])
        self.assertNotIn("data", definition["properties"])

        hit = self.schema["$defs"]["QueryHit"]
        self.assertEqual(
            {
                "object_type",
                "object_id",
                "projection_ref",
                "matched_fields",
                "summary",
                "record",
            },
            set(hit["required"]),
        )
        self.assertEqual(
            ["object", "null"],
            hit["properties"]["record"]["type"],
        )

    def test_rebase_hint_is_explicitly_advisory_and_never_auto_applied(self) -> None:
        definition = self.schema["$defs"]["RebaseHint"]
        self.assertEqual(
            "rebase-hint@1",
            definition["properties"]["hint_version"]["const"],
        )
        self.assertIs(True, definition["properties"]["advisory"]["const"])
        self.assertIs(
            False,
            definition["properties"]["safe_to_auto_apply"]["const"],
        )
        self.assertEqual(
            "REVIEW_AND_REBUILD",
            definition["properties"]["recommended_action"]["const"],
        )

    def test_positive_fixtures_validate(self) -> None:
        for name, fixture in POSITIVE_FIXTURES.items():
            with self.subTest(fixture=name):
                self.validator.validate(fixture)

        record_result = copy.deepcopy(QUERY_RESULT)
        record_result["normalized_query"] = copy.deepcopy(UNFILTERED_QUERY)
        record_result["hits"][0]["record"] = {
            "status": "ACTIVE",
            "claim": {
                "object_type": "CLAIM",
                "claim_id": "claim_atlas_postgresql_001",
            },
        }
        self.validator.validate(record_result)

    def test_negative_query_fixtures_reject_unknown_empty_and_unsupported_filters(
        self,
    ) -> None:
        cases: dict[str, dict[str, Any]] = {}

        unknown = copy.deepcopy(UNFILTERED_QUERY)
        unknown["unknown_filter"] = True
        cases["unknown_filter"] = unknown

        unsupported_kind = copy.deepcopy(UNFILTERED_QUERY)
        unsupported_kind["kinds"] = ["LEDGER_ENTRY"]
        cases["unsupported_kind"] = unsupported_kind

        duplicate_kind = copy.deepcopy(UNFILTERED_QUERY)
        duplicate_kind["kinds"] = ["CLAIM", "CLAIM"]
        cases["duplicate_kind"] = duplicate_kind

        for field in (
            "ids",
            "predicates",
            "source_ids",
            "source_revision_ids",
            "statuses",
        ):
            candidate = copy.deepcopy(UNFILTERED_QUERY)
            candidate[field] = [""]
            cases[f"empty_{field}"] = candidate

        empty_title = copy.deepcopy(UNFILTERED_QUERY)
        empty_title["title_contains"] = ""
        cases["empty_title_contains"] = empty_title

        for name, candidate in cases.items():
            with self.subTest(fixture=name):
                self.assertFalse(self.validator.is_valid(candidate))

    def test_negative_query_fixtures_reject_out_of_range_paging(self) -> None:
        cases = {}
        for name, field, value in (
            ("zero_limit", "limit", 0),
            ("over_maximum_limit", "limit", 1001),
            ("negative_offset", "offset", -1),
        ):
            candidate = copy.deepcopy(UNFILTERED_QUERY)
            candidate[field] = value
            cases[name] = candidate

        for name, candidate in cases.items():
            with self.subTest(fixture=name):
                self.assertFalse(self.validator.is_valid(candidate))

        for boundary in (1, 1000):
            with self.subTest(valid_limit=boundary):
                candidate = copy.deepcopy(UNFILTERED_QUERY)
                candidate["limit"] = boundary
                self.validator.validate(candidate)

    def test_negative_result_fixtures_reject_envelope_and_hit_drift(self) -> None:
        cases: dict[str, dict[str, Any]] = {}

        operation_result_wrapper = copy.deepcopy(QUERY_RESULT)
        operation_result_wrapper["ok"] = True
        cases["service_operation_result_field"] = operation_result_wrapper

        missing_normalized_query = copy.deepcopy(QUERY_RESULT)
        del missing_normalized_query["normalized_query"]
        cases["missing_normalized_query"] = missing_normalized_query

        unknown_hit_field = copy.deepcopy(QUERY_RESULT)
        unknown_hit_field["hits"][0]["score"] = 1.0
        cases["unknown_hit_field"] = unknown_hit_field

        missing_summary = copy.deepcopy(QUERY_RESULT)
        del missing_summary["hits"][0]["summary"]
        cases["missing_summary"] = missing_summary

        invalid_record = copy.deepcopy(QUERY_RESULT)
        invalid_record["hits"][0]["record"] = []
        cases["record_is_not_object_or_null"] = invalid_record

        for name, candidate in cases.items():
            with self.subTest(fixture=name):
                self.assertFalse(self.validator.is_valid(candidate))

    def test_negative_rebase_fixtures_reject_unsafe_or_ambiguous_advice(self) -> None:
        cases: dict[str, dict[str, Any]] = {}
        for name, field, value in (
            ("auto_apply_true", "safe_to_auto_apply", True),
            ("not_advisory", "advisory", False),
            ("wrong_action", "recommended_action", "APPLY_AUTOMATICALLY"),
        ):
            candidate = copy.deepcopy(REBASE_HINT)
            candidate[field] = value
            cases[name] = candidate

        unknown = copy.deepcopy(REBASE_HINT)
        unknown["replacement_proposal"] = {"operations": []}
        cases["unknown_top_level_field"] = unknown

        unknown_failed_field = copy.deepcopy(REBASE_HINT)
        unknown_failed_field["failed_precondition"]["mutation"] = "apply"
        cases["unknown_failed_precondition_field"] = unknown_failed_field

        unknown_replacement_field = copy.deepcopy(REBASE_HINT)
        unknown_replacement_field["replacement_preconditions"][0]["apply"] = True
        cases["unknown_replacement_precondition_field"] = unknown_replacement_field

        for name, candidate in cases.items():
            with self.subTest(fixture=name):
                self.assertFalse(self.validator.is_valid(candidate))


if __name__ == "__main__":
    unittest.main()
