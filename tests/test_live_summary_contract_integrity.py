from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from evals.product_continuity import runner


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_PATH = (
    ROOT
    / "evals"
    / "product_continuity"
    / "results"
    / "codex-live-summary.v3.json"
)


class LiveSummaryContractIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.historical = json.loads(HISTORICAL_PATH.read_text(encoding="utf-8"))

    def assert_invalid(self, summary: object) -> str:
        with self.assertRaisesRegex(ValueError, "INVALID_LIVE_SUMMARY") as raised:
            runner.live_summary_comparison(summary)  # type: ignore[arg-type]
        return str(raised.exception)

    def test_non_mapping_and_missing_required_metadata_fail_closed(self) -> None:
        for invalid in (None, [], "summary", 42):
            with self.subTest(invalid=invalid):
                self.assert_invalid(invalid)

        for field in (
            "artifact_version",
            "scenario_id",
            "project_snapshot_digest",
            "provider",
            "model_snapshot",
            "client",
            "tokenizer",
            "prompt_template_sha256",
            "response_schema_sha256",
            "settings",
            "arms",
            "redaction_attestation",
        ):
            with self.subTest(field=field):
                summary = copy.deepcopy(self.historical)
                summary.pop(field)
                self.assert_invalid(summary)

    def test_unknown_top_level_fields_cannot_hide_private_content(self) -> None:
        for field, value in (
            ("api_key", "sk-live-secret-value"),
            ("raw_prompt", "private prompt content"),
            ("private_note", "operator-only note"),
        ):
            with self.subTest(field=field):
                summary = copy.deepcopy(self.historical)
                summary[field] = value
                message = self.assert_invalid(summary)
                self.assertNotIn(value, message)

    def test_identity_hash_version_and_redaction_fields_are_enforced(self) -> None:
        cases = (
            ("artifact_version", "product-continuity-live-summary@999"),
            ("scenario_id", "BAD ID"),
            ("project_snapshot_digest", "sha256:bad"),
            ("provider", "Unknown Provider"),
            ("model_snapshot", "latest"),
            ("client", "floating"),
            ("tokenizer", "floating"),
            ("prompt_template_sha256", "sha256:bad"),
            ("response_schema_sha256", "sha256:bad"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                summary = copy.deepcopy(self.historical)
                summary[field] = value
                self.assert_invalid(summary)

        for field in ("reviewer", "reviewed_at", "statement"):
            with self.subTest(redaction_field=field):
                summary = copy.deepcopy(self.historical)
                summary["redaction_attestation"].pop(field)
                self.assert_invalid(summary)

    def test_settings_arms_and_nested_reports_are_closed(self) -> None:
        cases = []

        settings_secret = copy.deepcopy(self.historical)
        settings_secret["settings"]["authorization"] = "Bearer private"
        cases.append(settings_secret)

        extra_arm = copy.deepcopy(self.historical)
        extra_arm["arms"]["shadow"] = copy.deepcopy(
            extra_arm["arms"]["context_only"]
        )
        cases.append(extra_arm)

        nested_prompt = copy.deepcopy(self.historical)
        nested_prompt["arms"]["context_only"]["report"]["raw_prompt"] = (
            "private prompt"
        )
        cases.append(nested_prompt)

        invalid_schema_status = copy.deepcopy(self.historical)
        invalid_schema_status["arms"]["context_only"]["schema_validation"] = (
            "SKIPPED"
        )
        cases.append(invalid_schema_status)

        for index, summary in enumerate(cases):
            with self.subTest(index=index):
                self.assert_invalid(summary)

    def test_missing_required_arm_fields_never_leak_raw_key_errors(self) -> None:
        for field in ("input_bytes", "input_tokens", "elapsed_time_seconds"):
            with self.subTest(field=field):
                summary = copy.deepcopy(self.historical)
                summary["arms"]["manual_baseline"].pop(field)
                with self.assertRaisesRegex(
                    ValueError, "INVALID_LIVE_COMPARISON_METRIC"
                ):
                    runner.live_summary_comparison(summary)

        for field in ("schema_validation", "report"):
            with self.subTest(field=field):
                summary = copy.deepcopy(self.historical)
                summary["arms"]["manual_baseline"].pop(field)
                self.assert_invalid(summary)

    def test_precomparison_input_may_omit_comparison_but_cannot_forge_it(self) -> None:
        precomparison = copy.deepcopy(self.historical)
        precomparison.pop("comparison")
        self.assertEqual(
            runner.live_summary_comparison(self.historical),
            runner.live_summary_comparison(precomparison),
        )

        malformed = copy.deepcopy(self.historical)
        malformed["comparison"] = "forged-pass"
        self.assert_invalid(malformed)

    def test_historical_v1_and_current_v2_outputs_remain_exact(self) -> None:
        legacy = runner.live_summary_comparison(
            self.historical,
            comparison_version=runner.LIVE_COMPARISON_V1,
        )
        current = runner.live_summary_comparison(
            self.historical,
            comparison_version=runner.LIVE_COMPARISON_V2,
        )

        self.assertEqual(self.historical["comparison"], legacy)
        self.assertEqual(
            "product-continuity-live-comparison@2",
            current["report_version"],
        )
        self.assertEqual(legacy["passed"], current["passed"])


if __name__ == "__main__":
    unittest.main()
