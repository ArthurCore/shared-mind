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
HISTORICAL = json.loads(
    (EVAL_ROOT / "results" / "codex-live-summary.v3.json").read_text(
        encoding="utf-8"
    )
)


class EvaluationInputIntegrityTest(unittest.TestCase):
    def test_offline_empty_quality_cannot_vacuously_pass(self) -> None:
        scenario = copy.deepcopy(SCENARIO)
        scenario["metrics"]["quality"]["manual_baseline"] = {}
        scenario["metrics"]["quality"]["context_only"] = {}

        with self.assertRaisesRegex(ValueError, "INVALID_OFFLINE_QUALITY_METRICS"):
            runner.evaluate_scenario(scenario, scenario["expected_response"])

    def test_offline_metric_version_and_threshold_are_pinned(self) -> None:
        cases = (
            ("metric_version", "product-continuity-metrics@999", "INVALID_OFFLINE_METRIC_VERSION"),
            ("minimum_reduction_fraction", -1.0, "INVALID_OFFLINE_REDUCTION_THRESHOLD"),
            ("minimum_reduction_fraction", math.nan, "INVALID_OFFLINE_REDUCTION_THRESHOLD"),
            ("minimum_reduction_fraction", True, "INVALID_OFFLINE_REDUCTION_THRESHOLD"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                scenario = copy.deepcopy(SCENARIO)
                scenario["metrics"][field] = value
                with self.assertRaisesRegex(ValueError, code):
                    runner.evaluate_scenario(scenario, scenario["expected_response"])

    def test_offline_quality_fields_are_exact_finite_fractions(self) -> None:
        cases = (
            ({"fact_accuracy": 1.0},),
            (
                {
                    "fact_accuracy": 1.0,
                    "open_conflict_member_recall": 1.0,
                    "unexpected": 1.0,
                },
            ),
            (
                {
                    "fact_accuracy": True,
                    "open_conflict_member_recall": 1.0,
                },
            ),
            (
                {
                    "fact_accuracy": math.inf,
                    "open_conflict_member_recall": 1.0,
                },
            ),
            (
                {
                    "fact_accuracy": -0.1,
                    "open_conflict_member_recall": 1.0,
                },
            ),
        )
        for (quality,) in cases:
            with self.subTest(quality=quality):
                scenario = copy.deepcopy(SCENARIO)
                scenario["metrics"]["quality"]["context_only"] = quality
                with self.assertRaisesRegex(
                    ValueError, "INVALID_OFFLINE_QUALITY_METRICS"
                ):
                    runner.evaluate_scenario(scenario, scenario["expected_response"])

    def test_live_report_passed_flag_must_be_boolean(self) -> None:
        for invalid in ("false", 0, 1, None):
            with self.subTest(invalid=invalid):
                summary = copy.deepcopy(HISTORICAL)
                summary["arms"]["context_only"]["report"]["passed"] = invalid
                with self.assertRaisesRegex(ValueError, "INVALID_LIVE_REPORT_PASSED"):
                    runner.live_summary_comparison(summary)

    def test_live_nested_report_version_is_supported(self) -> None:
        summary = copy.deepcopy(HISTORICAL)
        summary["arms"]["manual_baseline"]["report"]["report_version"] = (
            "product-continuity-report@999"
        )

        with self.assertRaisesRegex(ValueError, "INVALID_LIVE_REPORT_VERSION"):
            runner.live_summary_comparison(summary)

    def test_live_report_quality_fields_are_bounded_numbers(self) -> None:
        cases = (
            ("score", True),
            ("score", -1),
            ("score", 101),
            ("fact_accuracy", math.nan),
            ("fact_accuracy", 1.1),
            ("open_conflict_member_recall", "1.0"),
        )
        for field, invalid in cases:
            with self.subTest(field=field, invalid=invalid):
                summary = copy.deepcopy(HISTORICAL)
                summary["arms"]["manual_baseline"]["report"][field] = invalid
                with self.assertRaisesRegex(ValueError, "INVALID_LIVE_REPORT_QUALITY"):
                    runner.live_summary_comparison(summary)


if __name__ == "__main__":
    unittest.main()
