from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


class ContinuityContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (CONTRACTS / "shared-mind-kernel.schema.v1.json").read_text(encoding="utf-8")
        )
        cls.fixtures = json.loads(
            (CONTRACTS / "atlas-conformance-fixtures.v1.json").read_text(encoding="utf-8")
        )
        cls.validator = Draft202012Validator(
            cls.schema,
            format_checker=FormatChecker(),
        )
        cls.objects = {
            item["name"]: item["object"] for item in cls.fixtures["typed_objects"]
        }

    def test_draft_2020_12_schema_defines_all_continuity_records(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        definitions = self.schema["$defs"]

        for name in ("DecisionRecord", "OpenQuestion", "WorkItem"):
            with self.subTest(definition=name):
                self.assertIn(name, definitions)
                self.assertIn("status", definitions[name]["required"])
                self.assertIn("version", definitions[name]["required"])

        top_level_refs = {item["$ref"] for item in self.schema["oneOf"]}
        self.assertTrue(
            {
                "#/$defs/DecisionRecord",
                "#/$defs/OpenQuestion",
                "#/$defs/WorkItem",
            }.issubset(top_level_refs)
        )

    def test_positive_fixtures_cover_every_continuity_record_and_operation(self) -> None:
        expected_records = {
            "decision_record_database_strategy",
            "open_question_cutover_window",
            "work_item_prepare_migration",
        }
        expected_proposals = {
            "record_decision_proposal": "RECORD_DECISION",
            "supersede_decision_proposal": "SUPERSEDE_DECISION",
            "open_question_proposal": "OPEN_QUESTION",
            "answer_question_proposal": "ANSWER_QUESTION",
            "drop_question_proposal": "DROP_QUESTION",
            "create_work_item_proposal": "CREATE_WORK_ITEM",
            "update_work_item_status_proposal": "UPDATE_WORK_ITEM_STATUS",
        }

        for name in expected_records | set(expected_proposals):
            with self.subTest(fixture=name):
                self.assertIn(name, self.objects)
                self.validator.validate(self.objects[name])

        for name, operation_name in expected_proposals.items():
            with self.subTest(proposal=name):
                operations = self.objects[name]["operations"]
                self.assertEqual([operation_name], [item["op"] for item in operations])

    def test_continuity_lifecycle_values_are_explicit(self) -> None:
        definitions = self.schema["$defs"]
        self.assertEqual(
            ["ACTIVE", "SUPERSEDED", "REVERSED"],
            definitions["DecisionRecord"]["properties"]["status"]["enum"],
        )
        self.assertEqual(
            ["OPEN", "ANSWERED", "DROPPED"],
            definitions["OpenQuestion"]["properties"]["status"]["enum"],
        )
        self.assertEqual(
            ["TODO", "DOING", "BLOCKED", "DONE", "DROPPED"],
            definitions["WorkItem"]["properties"]["status"]["enum"],
        )

        for name in (
            "decision_record_database_strategy",
            "open_question_cutover_window",
            "work_item_prepare_migration",
        ):
            with self.subTest(fixture=name):
                self.assertEqual(1, self.objects[name]["version"])

    def test_destructive_continuity_proposals_pin_version_and_status(self) -> None:
        cases = {
            "supersede_decision_proposal": (
                "DECISION_RECORD",
                "decision_record_database_strategy",
                "DECISION_STATUS_EQ",
                "DECISION_VERSION_EQ",
            ),
            "answer_question_proposal": (
                "OPEN_QUESTION",
                "open_question_cutover_window",
                "QUESTION_STATUS_EQ",
                "QUESTION_VERSION_EQ",
            ),
            "drop_question_proposal": (
                "OPEN_QUESTION",
                "open_question_cutover_window",
                "QUESTION_STATUS_EQ",
                "QUESTION_VERSION_EQ",
            ),
            "update_work_item_status_proposal": (
                "WORK_ITEM",
                "work_item_prepare_migration",
                "WORK_ITEM_STATUS_EQ",
                "WORK_ITEM_VERSION_EQ",
            ),
        }

        for fixture_name, expected in cases.items():
            with self.subTest(proposal=fixture_name):
                aggregate_type, fixture_ref, status_guard, version_guard = expected
                proposal = self.objects[fixture_name]
                target = self._record_id(self.objects[fixture_ref])
                self.assertIn(
                    {
                        "kind": "AGGREGATE",
                        "aggregate_type": aggregate_type,
                        "aggregate_id": target,
                        "expected_version": 1,
                    },
                    proposal["reads"],
                )
                guard_ops = {guard["op"] for guard in proposal["guards"]}
                self.assertIn(status_guard, guard_ops)
                self.assertIn(version_guard, guard_ops)

    def test_negative_fixtures_reject_missing_or_inconsistent_lifecycle_data(self) -> None:
        expected = {
            "decision_record_missing_version",
            "answered_question_missing_answer",
            "blocked_work_item_missing_blocker",
            "continuity_update_with_unknown_guard",
        }
        cases = {
            item["name"]: item for item in self.fixtures["negative_schema_cases"]
        }
        self.assertTrue(expected.issubset(cases))

        for name in expected:
            with self.subTest(case=name):
                case = cases[name]
                candidate = copy.deepcopy(self.objects[case["base_object"]])
                for field in case["remove_fields"]:
                    candidate.pop(field, None)
                candidate.update(copy.deepcopy(case["replace_fields"]))
                self.assertFalse(self.validator.is_valid(candidate))

    def test_ledger_events_cover_all_continuity_transitions(self) -> None:
        event_schema = self.schema["$defs"]["LedgerEvent"]
        event_types = {
            branch["properties"]["event_type"]["const"] for branch in event_schema["oneOf"]
        }
        self.assertTrue(
            {
                "DECISION_RECORDED",
                "DECISION_SUPERSEDED",
                "QUESTION_OPENED",
                "QUESTION_ANSWERED",
                "QUESTION_DROPPED",
                "WORK_ITEM_CREATED",
                "WORK_ITEM_STATUS_UPDATED",
            }.issubset(event_types)
        )

    def test_contract_validator_accepts_extended_fixture_set(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CONTRACTS / "validate_contract.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("continuity operations", result.stdout)

    @staticmethod
    def _record_id(record: dict[str, object]) -> object:
        for key in ("decision_id", "question_id", "work_item_id"):
            if key in record:
                return record[key]
        raise AssertionError(f"Fixture has no continuity record id: {record}")


if __name__ == "__main__":
    unittest.main()
