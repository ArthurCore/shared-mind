from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from evals.product_continuity import runner


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "product_continuity"
SCENARIO_PATH = EVAL_ROOT / "golden-atlas-continuity.v1.json"
REPORT_V1_PATH = EVAL_ROOT / "product-continuity-report.schema.v1.json"
REPORT_V2_PATH = EVAL_ROOT / "product-continuity-report.schema.v2.json"
LIVE_SUMMARY_SCHEMA_PATH = (
    EVAL_ROOT / "product-continuity-live-summary.schema.v1.json"
)
HISTORICAL_SUMMARY_PATH = (
    EVAL_ROOT / "results" / "codex-live-summary.v3.json"
)


class OfflineReductionIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        cls.report_v1 = json.loads(REPORT_V1_PATH.read_text(encoding="utf-8"))
        cls.live_summary_schema = json.loads(
            LIVE_SUMMARY_SCHEMA_PATH.read_text(encoding="utf-8")
        )
        cls.historical_summary = json.loads(
            HISTORICAL_SUMMARY_PATH.read_text(encoding="utf-8")
        )

    def regression_scenario(self) -> dict[str, object]:
        scenario = copy.deepcopy(self.scenario)
        scenario["metrics"]["manual_baseline"] = {
            "bytes": 100,
            "tokens": 100,
            "time_seconds": 10.0,
        }
        scenario["metrics"]["context_only"] = {
            "bytes": 125,
            "tokens": 150,
            "time_seconds": 12.5,
        }
        return scenario

    def test_default_v2_report_preserves_negative_reductions(self) -> None:
        scenario = self.regression_scenario()

        report = runner.evaluate_scenario(
            scenario,
            scenario["expected_response"],
        )

        self.assertEqual("product-continuity-report@2", report["report_version"])
        self.assertEqual(
            {
                "bytes": -0.25,
                "tokens": -0.5,
                "time_seconds": -0.25,
            },
            report["metric_comparison"]["reductions"],
        )
        self.assertFalse(report["metric_comparison"]["meets_reduction_target"])
        self.assertFalse(report["passed"])
        schema = json.loads(REPORT_V2_PATH.read_text(encoding="utf-8"))
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(report)))

    def test_explicit_v1_report_reproduces_historical_clamp(self) -> None:
        scenario = self.regression_scenario()

        report = runner.evaluate_scenario(
            scenario,
            scenario["expected_response"],
            report_version="product-continuity-report@1",
        )

        self.assertEqual("product-continuity-report@1", report["report_version"])
        self.assertEqual(
            {"bytes": 0.0, "tokens": 0.0, "time_seconds": 0.0},
            report["metric_comparison"]["reductions"],
        )
        self.assertEqual(
            [],
            list(Draft202012Validator(self.report_v1).iter_errors(report)),
        )

    def test_scenario_pins_the_v2_report_schema(self) -> None:
        self.assertEqual(
            "product-continuity-report.schema.v2.json",
            self.scenario["report_schema"],
        )
        self.assertTrue(REPORT_V2_PATH.is_file())
        Draft202012Validator.check_schema(
            json.loads(REPORT_V2_PATH.read_text(encoding="utf-8"))
        )

    def test_live_summary_dispatches_nested_report_reduction_semantics(self) -> None:
        scenario = self.regression_scenario()
        report = runner.evaluate_scenario(
            scenario,
            scenario["expected_response"],
        )
        summary = copy.deepcopy(self.historical_summary)
        summary["arms"]["manual_baseline"]["report"] = report
        summary["arms"]["context_only"]["report"] = report
        summary["comparison"] = runner.live_summary_comparison(summary)
        validator = Draft202012Validator(self.live_summary_schema)

        self.assertEqual([], list(validator.iter_errors(summary)))

        mislabeled = copy.deepcopy(summary)
        mislabeled["arms"]["context_only"]["report"]["report_version"] = (
            "product-continuity-report@1"
        )
        self.assertNotEqual([], list(validator.iter_errors(mislabeled)))

    def test_unknown_version_and_invalid_metrics_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "UNSUPPORTED_PRODUCT_CONTINUITY_REPORT_VERSION",
        ):
            runner.evaluate_scenario(
                self.scenario,
                self.scenario["expected_response"],
                report_version="product-continuity-report@999",
            )

        for invalid in (0, -1, math.inf, math.nan, True):
            with self.subTest(invalid=invalid):
                scenario = copy.deepcopy(self.scenario)
                scenario["metrics"]["manual_baseline"]["bytes"] = invalid
                with self.assertRaisesRegex(
                    ValueError,
                    "INVALID_OFFLINE_COMPARISON_METRIC",
                ):
                    runner.evaluate_scenario(
                        scenario,
                        scenario["expected_response"],
                    )


if __name__ == "__main__":
    unittest.main()
