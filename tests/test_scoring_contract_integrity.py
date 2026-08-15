from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path

from evals.product_continuity import runner


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "product_continuity"
SCENARIO = json.loads(
    (EVAL_ROOT / "golden-atlas-continuity.v1.json").read_text(encoding="utf-8")
)


class ScoringContractIntegrityTest(unittest.TestCase):
    def malformed_response(self) -> dict[str, object]:
        response = copy.deepcopy(SCENARIO["expected_response"])
        response["project_purpose"] = "wrong"
        response["current_decisions"] = []
        response["settled_claims"] = []
        response["open_questions"] = []
        response["actionable_work_items"] = []
        return response

    def test_weakened_quality_thresholds_cannot_pass_bad_response(self) -> None:
        scenario = copy.deepcopy(SCENARIO)
        scenario["scoring"]["passing_score"] = -1
        scenario["scoring"]["required_fact_accuracy"] = -1.0
        scenario["scoring"]["required_open_conflict_member_recall"] = -1.0

        with self.assertRaisesRegex(ValueError, "INVALID_SCORING_CONTRACT"):
            runner.evaluate_scenario(scenario, self.malformed_response())

    def test_score_thresholds_are_exact_typed_constants(self) -> None:
        cases = (
            ("maximum_score", 99),
            ("maximum_score", True),
            ("passing_score", 0),
            ("passing_score", True),
            ("required_fact_accuracy", 0.0),
            ("required_fact_accuracy", True),
            ("required_fact_accuracy", math.nan),
            ("required_open_conflict_member_recall", 0.0),
        )
        for field, invalid in cases:
            with self.subTest(field=field, invalid=invalid):
                scenario = copy.deepcopy(SCENARIO)
                scenario["scoring"][field] = invalid
                with self.assertRaisesRegex(ValueError, "INVALID_SCORING_CONTRACT"):
                    runner.evaluate_scenario(scenario, scenario["expected_response"])

    def test_dimension_weights_are_exact_and_complete(self) -> None:
        cases = (
            {**SCENARIO["scoring"]["dimensions"], "project_purpose": 100},
            {
                key: value
                for key, value in SCENARIO["scoring"]["dimensions"].items()
                if key != "project_purpose"
            },
            {**SCENARIO["scoring"]["dimensions"], "unexpected": 0},
            {**SCENARIO["scoring"]["dimensions"], "project_purpose": True},
        )
        for dimensions in cases:
            with self.subTest(dimensions=dimensions):
                scenario = copy.deepcopy(SCENARIO)
                scenario["scoring"]["dimensions"] = dimensions
                with self.assertRaisesRegex(ValueError, "INVALID_SCORING_CONTRACT"):
                    runner.evaluate_scenario(scenario, scenario["expected_response"])

    def test_penalties_are_exact_positive_constants(self) -> None:
        cases = (
            {**SCENARIO["scoring"]["penalties"], "HALLUCINATED_ID": -1000},
            {
                key: value
                for key, value in SCENARIO["scoring"]["penalties"].items()
                if key != "HALLUCINATED_ID"
            },
            {**SCENARIO["scoring"]["penalties"], "unexpected": 1},
            {**SCENARIO["scoring"]["penalties"], "HALLUCINATED_ID": True},
        )
        for penalties in cases:
            with self.subTest(penalties=penalties):
                scenario = copy.deepcopy(SCENARIO)
                scenario["scoring"]["penalties"] = penalties
                with self.assertRaisesRegex(ValueError, "INVALID_SCORING_CONTRACT"):
                    runner.evaluate_scenario(scenario, scenario["expected_response"])

    def test_scoring_field_set_is_exact(self) -> None:
        for mutate in (
            lambda scoring: scoring.pop("passing_score"),
            lambda scoring: scoring.__setitem__("unexpected", 1),
        ):
            scenario = copy.deepcopy(SCENARIO)
            mutate(scenario["scoring"])
            with self.assertRaisesRegex(ValueError, "INVALID_SCORING_CONTRACT"):
                runner.evaluate_scenario(scenario, scenario["expected_response"])

    def test_valid_scenario_output_is_unchanged(self) -> None:
        report = runner.evaluate_scenario(SCENARIO, SCENARIO["expected_response"])

        self.assertEqual(100, report["score"])
        self.assertEqual(100, report["maximum_score"])
        self.assertEqual(SCENARIO["scoring"]["dimensions"], report["dimension_scores"])
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
