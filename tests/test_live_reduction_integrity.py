from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from evals.product_continuity import runner


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "product_continuity"
SUMMARY_SCHEMA_PATH = (
    EVAL_ROOT / "product-continuity-live-summary.schema.v1.json"
)
HISTORICAL_SUMMARY_PATH = (
    EVAL_ROOT / "results" / "codex-live-summary.v3.json"
)


class LiveReductionIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SUMMARY_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(
            cls.schema,
            format_checker=FormatChecker(),
        )
        cls.historical = json.loads(
            HISTORICAL_SUMMARY_PATH.read_text(encoding="utf-8")
        )

    def test_v2_preserves_negative_resource_reductions(self) -> None:
        summary = copy.deepcopy(self.historical)
        manual = summary["arms"]["manual_baseline"]
        context = summary["arms"]["context_only"]
        manual.update(
            input_bytes=100,
            input_tokens=100,
            elapsed_time_seconds=10.0,
        )
        context.update(
            input_bytes=125,
            input_tokens=150,
            elapsed_time_seconds=12.5,
        )

        comparison = runner.live_summary_comparison(summary)
        summary["comparison"] = comparison

        self.assertEqual(
            "product-continuity-live-comparison@2",
            comparison["report_version"],
        )
        self.assertEqual(
            {
                "bytes": -0.25,
                "tokens": -0.5,
                "time_seconds": -0.25,
            },
            comparison["reductions"],
        )
        self.assertFalse(comparison["meets_reduction_target"])
        self.assertFalse(comparison["passed"])
        self.assertEqual([], list(self.validator.iter_errors(summary)))

    def test_historical_v1_artifact_remains_byte_stable_and_reproducible(
        self,
    ) -> None:
        original_bytes = HISTORICAL_SUMMARY_PATH.read_bytes()

        comparison = runner.live_summary_comparison(
            self.historical,
            comparison_version="product-continuity-live-comparison@1",
        )

        self.assertEqual(self.historical["comparison"], comparison)
        self.assertEqual(original_bytes, HISTORICAL_SUMMARY_PATH.read_bytes())
        self.assertEqual([], list(self.validator.iter_errors(self.historical)))

    def test_v1_and_v2_make_regression_visibility_explicit(self) -> None:
        summary = copy.deepcopy(self.historical)
        summary["arms"]["manual_baseline"]["input_tokens"] = 100
        summary["arms"]["context_only"]["input_tokens"] = 150

        legacy = runner.live_summary_comparison(
            summary,
            comparison_version="product-continuity-live-comparison@1",
        )
        current = runner.live_summary_comparison(
            summary,
            comparison_version="product-continuity-live-comparison@2",
        )

        self.assertEqual(0.0, legacy["reductions"]["tokens"])
        self.assertEqual(-0.5, current["reductions"]["tokens"])

    def test_unknown_version_and_invalid_metrics_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "UNSUPPORTED_LIVE_COMPARISON_VERSION",
        ):
            runner.live_summary_comparison(
                self.historical,
                comparison_version="product-continuity-live-comparison@999",
            )

        for invalid in (0, -1, math.inf, math.nan, True):
            with self.subTest(invalid=invalid):
                summary = copy.deepcopy(self.historical)
                summary["arms"]["manual_baseline"]["input_bytes"] = invalid
                with self.assertRaisesRegex(
                    ValueError,
                    "INVALID_LIVE_COMPARISON_METRIC",
                ):
                    runner.live_summary_comparison(summary)


if __name__ == "__main__":
    unittest.main()
