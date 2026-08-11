from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from shared_mind.continuity import (
    ContinuityConflict,
    ContinuityValidationError,
    RequiredRead,
    apply_operation,
    create_schema,
    required_reads,
    state_rows,
    validate_guard,
    validate_read,
)


ROOT = Path(__file__).resolve().parents[1]


class ContinuityRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        schema = json.loads(
            (ROOT / "contracts" / "shared-mind-kernel.schema.v1.json").read_text(
                encoding="utf-8"
            )
        )
        cls.event_validator = Draft202012Validator(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$defs": schema["$defs"],
                "$ref": "#/$defs/LedgerEvent",
            },
            format_checker=FormatChecker(),
        )
        fixtures = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        cls.objects = {
            item["name"]: item["object"] for item in fixtures["typed_objects"]
        }

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def operation(self, fixture_name: str) -> dict[str, Any]:
        return copy.deepcopy(self.objects[fixture_name]["operations"][0])

    def test_schema_creation_is_idempotent_and_exposes_expected_tables(self) -> None:
        create_schema(self.connection)

        names = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        self.assertTrue(
            {"decision_records", "open_questions", "work_items"}.issubset(names)
        )
        self.assertEqual(
            {
                "decision_records": [],
                "open_questions": [],
                "work_items": [],
            },
            state_rows(self.connection),
        )

    def test_required_reads_are_derived_from_every_destructive_operation(self) -> None:
        cases = {
            "supersede_decision_proposal": RequiredRead(
                "DECISION_RECORD", "decision_atlas_database_strategy_001"
            ),
            "answer_question_proposal": RequiredRead(
                "OPEN_QUESTION", "question_atlas_cutover_window_001"
            ),
            "drop_question_proposal": RequiredRead(
                "OPEN_QUESTION", "question_atlas_cutover_window_001"
            ),
            "update_work_item_status_proposal": RequiredRead(
                "WORK_ITEM", "workitem_prepare_atlas_migration_001"
            ),
        }

        for fixture_name, expected in cases.items():
            with self.subTest(operation=fixture_name):
                self.assertEqual(
                    (expected,), required_reads(self.operation(fixture_name))
                )

        for fixture_name in (
            "record_decision_proposal",
            "open_question_proposal",
            "create_work_item_proposal",
        ):
            with self.subTest(operation=fixture_name):
                self.assertEqual((), required_reads(self.operation(fixture_name)))

    def test_records_and_supersedes_decisions_without_erasing_history(self) -> None:
        events: list[dict[str, Any]] = []
        record = self.operation("record_decision_proposal")
        supersede = self.operation("supersede_decision_proposal")

        self.assertTrue(apply_operation(self.connection, record, events))
        self.assertEqual(
            [{"event_type": "DECISION_RECORDED", "decision": record["decision"]}],
            events,
        )

        self.assertTrue(apply_operation(self.connection, supersede, events))

        rows = state_rows(self.connection)["decision_records"]
        self.assertEqual(
            [
                "decision_atlas_database_strategy_001",
                "decision_atlas_database_strategy_002",
            ],
            [row["decision_id"] for row in rows],
        )
        prior = rows[0]
        replacement = rows[1]
        self.assertEqual("SUPERSEDED", prior["status"])
        self.assertEqual(2, prior["version"])
        self.assertEqual(
            replacement["decision_id"], prior["replaced_by_decision_id"]
        )
        self.assertEqual("SUPERSEDED", prior["document"]["status"])
        self.assertEqual(2, prior["document"]["version"])
        self.assertEqual("ACTIVE", replacement["status"])
        self.assertEqual(1, replacement["version"])
        self.assertEqual(
            {
                "event_type": "DECISION_SUPERSEDED",
                "target_decision_id": prior["decision_id"],
                "target_disposition": "SUPERSEDED",
                "replacement_decision": supersede["replacement_decision"],
                "rationale": supersede["rationale"],
                "previous_version": 1,
                "new_version": 2,
            },
            events[-1],
        )

    def test_decision_lifecycle_rejects_invalid_initial_and_non_active_targets(self) -> None:
        record = self.operation("record_decision_proposal")
        invalid = copy.deepcopy(record)
        invalid["decision"]["status"] = "SUPERSEDED"
        invalid["decision"]["version"] = 2
        invalid["decision"]["replaced_by_decision_id"] = "decision_other_001"

        with self.assertRaises(ContinuityValidationError) as caught:
            apply_operation(self.connection, invalid, [])
        self.assertEqual("INVALID_NEW_DECISION_STATE", caught.exception.code)

        apply_operation(self.connection, record, [])
        supersede = self.operation("supersede_decision_proposal")
        apply_operation(self.connection, supersede, [])
        second = copy.deepcopy(supersede)
        second["replacement_decision"]["decision_id"] = "decision_replacement_003"

        with self.assertRaises(ContinuityConflict) as caught:
            apply_operation(self.connection, second, [])
        self.assertEqual("DECISION_STATUS_MISMATCH", caught.exception.code)

    def test_decision_supersede_rejects_an_unknown_target_disposition(self) -> None:
        apply_operation(
            self.connection, self.operation("record_decision_proposal"), []
        )
        supersede = self.operation("supersede_decision_proposal")
        supersede["target_disposition"] = "ARCHIVED"

        with self.assertRaises(ContinuityValidationError) as caught:
            apply_operation(self.connection, supersede, [])

        self.assertEqual("INVALID_DECISION_DISPOSITION", caught.exception.code)
        rows = state_rows(self.connection)["decision_records"]
        self.assertEqual(1, len(rows))
        self.assertEqual("ACTIVE", rows[0]["status"])

    def test_answers_or_drops_only_open_questions_and_increments_version(self) -> None:
        open_operation = self.operation("open_question_proposal")
        answer_operation = self.operation("answer_question_proposal")
        events: list[dict[str, Any]] = []
        apply_operation(self.connection, open_operation, events)
        apply_operation(self.connection, answer_operation, events)

        answered = state_rows(self.connection)["open_questions"][0]
        self.assertEqual("ANSWERED", answered["status"])
        self.assertEqual(2, answered["version"])
        self.assertEqual(answer_operation["answer"], answered["answer"])
        self.assertIsNone(answered["drop"])
        self.assertEqual(
            {
                "event_type": "QUESTION_ANSWERED",
                "target_question_id": answered["question_id"],
                "answer": answer_operation["answer"],
                "previous_version": 1,
                "new_version": 2,
            },
            events[-1],
        )

        with self.assertRaises(ContinuityConflict) as caught:
            apply_operation(
                self.connection, self.operation("drop_question_proposal"), []
            )
        self.assertEqual("QUESTION_STATUS_MISMATCH", caught.exception.code)

        other = sqlite3.connect(":memory:", isolation_level=None)
        other.row_factory = sqlite3.Row
        self.addCleanup(other.close)
        create_schema(other)
        apply_operation(other, open_operation, [])
        drop_operation = self.operation("drop_question_proposal")
        drop_events: list[dict[str, Any]] = []
        apply_operation(other, drop_operation, drop_events)
        dropped = state_rows(other)["open_questions"][0]
        self.assertEqual("DROPPED", dropped["status"])
        self.assertIsNone(dropped["answer"])
        self.assertEqual(drop_operation["drop"], dropped["drop"])
        self.assertEqual("QUESTION_DROPPED", drop_events[-1]["event_type"])

    def test_question_creation_enforces_open_version_one_state(self) -> None:
        operation = self.operation("open_question_proposal")
        operation["question"]["status"] = "ANSWERED"
        operation["question"]["version"] = 2
        operation["question"]["answer"] = self.operation(
            "answer_question_proposal"
        )["answer"]

        with self.assertRaises(ContinuityValidationError) as caught:
            apply_operation(self.connection, operation, [])

        self.assertEqual("INVALID_NEW_QUESTION_STATE", caught.exception.code)

    def test_work_item_status_updates_preserve_blocker_invariant_and_history(self) -> None:
        create = self.operation("create_work_item_proposal")
        update = self.operation("update_work_item_status_proposal")
        events: list[dict[str, Any]] = []
        apply_operation(self.connection, create, events)
        apply_operation(self.connection, update, events)

        row = state_rows(self.connection)["work_items"][0]
        self.assertEqual("BLOCKED", row["status"])
        self.assertEqual(2, row["version"])
        self.assertEqual(update["blocker"], row["blocker"])
        self.assertEqual(update["updated_at"], row["updated_at"])
        self.assertEqual(
            {
                "event_type": "WORK_ITEM_STATUS_UPDATED",
                "target_work_item_id": row["work_item_id"],
                "previous_status": "TODO",
                "new_status": "BLOCKED",
                "blocker": update["blocker"],
                "rationale": update["rationale"],
                "updated_by": update["updated_by"],
                "updated_at": update["updated_at"],
                "previous_version": 1,
                "new_version": 2,
            },
            events[-1],
        )

        invalid_blocked = copy.deepcopy(update)
        invalid_blocked["blocker"] = None
        with self.assertRaises(ContinuityValidationError) as caught:
            apply_operation(self.connection, invalid_blocked, [])
        self.assertEqual("INVALID_WORK_ITEM_BLOCKER", caught.exception.code)

        invalid_unblocked = copy.deepcopy(update)
        invalid_unblocked["new_status"] = "DOING"
        with self.assertRaises(ContinuityValidationError) as caught:
            apply_operation(self.connection, invalid_unblocked, [])
        self.assertEqual("INVALID_WORK_ITEM_BLOCKER", caught.exception.code)

    def test_work_item_creation_enforces_todo_version_one_state(self) -> None:
        operation = self.operation("create_work_item_proposal")
        operation["work_item"]["status"] = "BLOCKED"
        operation["work_item"]["version"] = 2
        operation["work_item"]["blocker"] = "Waiting for approval"

        with self.assertRaises(ContinuityValidationError) as caught:
            apply_operation(self.connection, operation, [])

        self.assertEqual("INVALID_NEW_WORK_ITEM_STATE", caught.exception.code)

    def test_reads_and_guards_detect_missing_stale_or_changed_aggregates(self) -> None:
        apply_operation(self.connection, self.operation("record_decision_proposal"), [])
        apply_operation(self.connection, self.operation("open_question_proposal"), [])
        apply_operation(self.connection, self.operation("create_work_item_proposal"), [])

        valid_read = {
            "kind": "AGGREGATE",
            "aggregate_type": "DECISION_RECORD",
            "aggregate_id": "decision_atlas_database_strategy_001",
            "expected_version": 1,
        }
        self.assertTrue(validate_read(self.connection, valid_read))
        self.assertFalse(
            validate_read(
                self.connection,
                {
                    "kind": "AGGREGATE",
                    "aggregate_type": "CLAIM",
                    "aggregate_id": "claim_other_001",
                    "expected_version": 1,
                },
            )
        )

        stale_cases = (
            (
                {**valid_read, "expected_version": 9},
                "DECISION_VERSION_MISMATCH",
            ),
            (
                {
                    "kind": "AGGREGATE",
                    "aggregate_type": "OPEN_QUESTION",
                    "aggregate_id": "question_missing_001",
                    "expected_version": 1,
                },
                "QUESTION_VERSION_MISMATCH",
            ),
            (
                {
                    "kind": "AGGREGATE",
                    "aggregate_type": "WORK_ITEM",
                    "aggregate_id": "workitem_prepare_atlas_migration_001",
                    "expected_version": 2,
                },
                "WORK_ITEM_VERSION_MISMATCH",
            ),
        )
        for read, expected_code in stale_cases:
            with self.subTest(read=read):
                with self.assertRaises(ContinuityConflict) as caught:
                    validate_read(self.connection, read)
                self.assertEqual(expected_code, caught.exception.code)

        guards = (
            (
                {
                    "op": "DECISION_STATUS_EQ",
                    "decision_id": "decision_atlas_database_strategy_001",
                    "expected_status": "SUPERSEDED",
                },
                "DECISION_STATUS_MISMATCH",
            ),
            (
                {
                    "op": "QUESTION_VERSION_EQ",
                    "question_id": "question_atlas_cutover_window_001",
                    "expected_version": 2,
                },
                "QUESTION_VERSION_MISMATCH",
            ),
            (
                {
                    "op": "WORK_ITEM_STATUS_EQ",
                    "work_item_id": "workitem_prepare_atlas_migration_001",
                    "expected_status": "DOING",
                },
                "WORK_ITEM_STATUS_MISMATCH",
            ),
        )
        for guard, expected_code in guards:
            with self.subTest(guard=guard):
                with self.assertRaises(ContinuityConflict) as caught:
                    validate_guard(self.connection, guard)
                self.assertEqual(expected_code, caught.exception.code)

        self.assertFalse(
            validate_guard(
                self.connection,
                {
                    "op": "CLAIM_STATUS_EQ",
                    "claim_id": "claim_other_001",
                    "expected_status": "ACTIVE",
                },
            )
        )

    def test_apply_operation_does_not_commit_the_callers_transaction(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

        apply_operation(
            self.connection, self.operation("record_decision_proposal"), []
        )

        self.assertTrue(self.connection.in_transaction)
        self.connection.execute("ROLLBACK")
        count = self.connection.execute(
            "SELECT COUNT(*) FROM decision_records"
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_every_emitted_event_conforms_to_the_ledger_event_contract(self) -> None:
        operation_sequences = (
            ("record_decision_proposal", "supersede_decision_proposal"),
            ("open_question_proposal", "answer_question_proposal"),
            ("open_question_proposal", "drop_question_proposal"),
            ("create_work_item_proposal", "update_work_item_status_proposal"),
        )
        emitted: list[dict[str, Any]] = []

        for sequence in operation_sequences:
            connection = sqlite3.connect(":memory:", isolation_level=None)
            self.addCleanup(connection.close)
            create_schema(connection)
            for fixture_name in sequence:
                apply_operation(connection, self.operation(fixture_name), emitted)

        for event in emitted:
            with self.subTest(event_type=event["event_type"]):
                self.event_validator.validate(event)

        self.assertEqual(
            {
                "DECISION_RECORDED",
                "DECISION_SUPERSEDED",
                "QUESTION_OPENED",
                "QUESTION_ANSWERED",
                "QUESTION_DROPPED",
                "WORK_ITEM_CREATED",
                "WORK_ITEM_STATUS_UPDATED",
            },
            {event["event_type"] for event in emitted},
        )

    def test_unknown_operations_are_left_for_the_kernel_dispatcher(self) -> None:
        events: list[dict[str, Any]] = []

        handled = apply_operation(
            self.connection,
            {"op": "ASSERT_CLAIM", "op_id": "operation_claim_001"},
            events,
        )

        self.assertFalse(handled)
        self.assertEqual([], events)


if __name__ == "__main__":
    unittest.main()
