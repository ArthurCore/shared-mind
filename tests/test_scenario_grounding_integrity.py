from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from evals.product_continuity import runner


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = json.loads(
    (ROOT / "evals" / "product_continuity" / "golden-atlas-continuity.v1.json")
    .read_text(encoding="utf-8")
)


class ScenarioGroundingIntegrityTest(unittest.TestCase):
    def assert_invalid(self, scenario: dict[str, object]) -> None:
        with self.assertRaisesRegex(ValueError, "INVALID_SCENARIO_CONTRACT"):
            runner.evaluate_scenario(scenario, scenario.get("expected_response", {}))

    def test_vacuous_scenario_cannot_receive_a_passing_report(self) -> None:
        scenario = copy.deepcopy(SCENARIO)
        scenario["context"] = {}
        scenario["expected_response"] = {}

        self.assert_invalid(scenario)

    def test_scenario_version_shape_and_schema_pins_are_exact(self) -> None:
        cases = (
            lambda value: value.__setitem__("scenario_version", "product-continuity-scenario@999"),
            lambda value: value.pop("description"),
            lambda value: value.__setitem__("unexpected", True),
            lambda value: value.__setitem__("response_schema", "attacker.schema.json"),
            lambda value: value.__setitem__("metrics_schema", "attacker.schema.json"),
            lambda value: value.__setitem__("report_schema", "product-continuity-report.schema.v1.json"),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                scenario = copy.deepcopy(SCENARIO)
                mutate(scenario)
                self.assert_invalid(scenario)

    def test_context_shape_and_scenario_identity_are_exact(self) -> None:
        cases = (
            lambda context: context.pop("purpose"),
            lambda context: context.__setitem__("unexpected", True),
            lambda context: context.__setitem__("evaluation_scenario_id", "other-scenario"),
            lambda context: context.__setitem__("purpose_missing", True),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                scenario = copy.deepcopy(SCENARIO)
                mutate(scenario["context"])
                self.assert_invalid(scenario)

    def test_expected_response_shape_identity_and_purpose_are_grounded(self) -> None:
        cases = (
            lambda expected: expected.pop("current_decisions"),
            lambda expected: expected.__setitem__("unexpected", True),
            lambda expected: expected.__setitem__("scenario_id", "other-scenario"),
            lambda expected: expected.__setitem__("project_purpose", "invented purpose"),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                scenario = copy.deepcopy(SCENARIO)
                mutate(scenario["expected_response"])
                self.assert_invalid(scenario)

    def test_every_scored_dimension_is_non_vacuous(self) -> None:
        for field in (
            "current_decisions",
            "settled_claims",
            "open_conflicts",
            "open_questions",
            "actionable_work_items",
        ):
            with self.subTest(field=field):
                scenario = copy.deepcopy(SCENARIO)
                scenario["expected_response"][field] = []
                self.assert_invalid(scenario)

    def test_expected_records_must_match_the_context_authority(self) -> None:
        cases = (
            lambda expected: expected["current_decisions"][0].__setitem__("conclusion", "invented"),
            lambda expected: expected["settled_claims"][0].__setitem__("proposition_hash", "sha256:" + "0" * 64),
            lambda expected: expected["open_conflicts"][0]["member_claims"].pop(),
            lambda expected: expected["open_questions"][0].__setitem__("question", "invented?"),
            lambda expected: expected["actionable_work_items"][0].__setitem__("status", "DONE"),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                scenario = copy.deepcopy(SCENARIO)
                mutate(scenario["expected_response"])
                self.assert_invalid(scenario)

    def test_valid_scenario_report_is_unchanged(self) -> None:
        report = runner.evaluate_scenario(SCENARIO, SCENARIO["expected_response"])

        self.assertEqual(100, report["score"])
        self.assertEqual(1.0, report["fact_accuracy"])
        self.assertEqual(1.0, report["open_conflict_member_recall"])
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
