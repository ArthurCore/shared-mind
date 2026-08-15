from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from evals.shared_state_continuity.runner import run_paired_evaluation
from shared_mind.canonical import sha256_json
from shared_mind.continuity_eval import (
    ContinuityEvaluationError,
    evaluate_paired_context_reduction,
)
from shared_mind.product import ProductError
from shared_mind.product_cli import main as product_cli_main
from shared_mind.product_mcp_server import ProductMcpApplication

from tests.product_support import ProductTestCase
from tests.test_continuity_evaluation import context, expectation, observation


REPORT_SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "evals"
    / "shared_state_continuity"
    / "report.schema.v1.json"
)


def measured_context(
    included_bytes: int, *, state_root: str = "sha256:" + "2" * 64
) -> dict:
    document = context()
    document["kernel_state_root"] = state_root
    document["budget"]["included_bytes"] = included_bytes
    document["context_hash"] = sha256_json(
        {key: value for key, value in document.items() if key != "context_hash"}
    )
    return document


def thresholds() -> dict:
    return {
        "threshold_version": "paired-context-reduction-thresholds@1",
        "min_context_bytes_reduction_rate": 0.4,
        "min_context_tokens_reduction_rate": 0.4,
        "min_time_to_productive_action_reduction_rate": 0.4,
    }


def evaluate_pair(**overrides: object) -> dict:
    values = {
        "baseline_context": measured_context(12_000),
        "baseline_observation": observation(),
        "candidate_context": measured_context(6_000),
        "candidate_observation": observation(),
        "expectation": expectation(),
        "thresholds": thresholds(),
        "baseline_elapsed_ms": 1_000,
        "candidate_elapsed_ms": 400,
        "baseline_token_count": 3_000,
        "candidate_token_count": 1_500,
    }
    values.update(overrides)
    return evaluate_paired_context_reduction(**values)


class PairedContextReductionUnitTest(unittest.TestCase):
    def test_dev_087_reports_exact_reductions_and_preserved_quality(self) -> None:
        report = evaluate_pair()

        self.assertEqual("paired-context-reduction-eval@1", report["report_version"])
        self.assertEqual("sha256:" + "2" * 64, report["kernel_state_root"])
        self.assertTrue(report["baseline"]["passed"])
        self.assertTrue(report["candidate"]["passed"])
        self.assertEqual(
            {
                "context_bytes_reduction_rate": 0.5,
                "context_tokens_reduction_rate": 0.5,
                "time_to_productive_action_reduction_rate": 0.6,
            },
            report["reductions"],
        )
        self.assertTrue(report["quality_preserved"])
        self.assertEqual([], report["failures"])
        self.assertTrue(report["passed"])
        self.assertEqual(
            report["report_hash"],
            sha256_json(
                {key: value for key, value in report.items() if key != "report_hash"}
            ),
        )

    def test_quality_regression_fails_even_when_context_is_smaller(self) -> None:
        degraded = observation()
        degraded["decision_ids"] = degraded["decision_ids"][1:]

        report = evaluate_pair(candidate_observation=degraded)

        self.assertFalse(report["quality_preserved"])
        self.assertIn("CANDIDATE_QUALITY_GATE_FAILED", report["failures"])
        self.assertIn("CANDIDATE_QUALITY_REGRESSION", report["failures"])
        self.assertFalse(report["passed"])

    def test_candidate_larger_than_baseline_is_reported_without_clamping(self) -> None:
        report = evaluate_pair(candidate_context=measured_context(13_000))

        self.assertLess(report["reductions"]["context_bytes_reduction_rate"], 0)
        self.assertIn("CONTEXT_BYTES_NOT_REDUCED", report["failures"])
        self.assertIn("CONTEXT_BYTES_REDUCTION_BELOW_THRESHOLD", report["failures"])
        self.assertFalse(report["passed"])

    def test_state_root_mismatch_fails_closed(self) -> None:
        with self.assertRaises(ContinuityEvaluationError) as caught:
            evaluate_pair(
                candidate_context=measured_context(
                    6_000, state_root="sha256:" + "3" * 64
                )
            )
        self.assertEqual("PAIRED_STATE_ROOT_MISMATCH", caught.exception.code)

    def test_malformed_or_unsafe_measurements_fail_closed(self) -> None:
        cases = (
            (
                {"thresholds": thresholds() | {"threshold_version": "unknown"}},
                "PAIRED_THRESHOLDS_INVALID",
            ),
            ({"baseline_token_count": 0}, "PAIRED_BASELINE_TOKEN_COUNT_INVALID"),
            ({"baseline_elapsed_ms": 0}, "PAIRED_BASELINE_ELAPSED_INVALID"),
            ({"candidate_token_count": -1}, "EVALUATION_NUMBER_INVALID"),
            ({"candidate_elapsed_ms": -1}, "EVALUATION_NUMBER_INVALID"),
        )
        for overrides, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ContinuityEvaluationError) as caught:
                    evaluate_pair(**overrides)
                self.assertEqual(code, caught.exception.code)

    def test_context_hash_tampering_is_rejected(self) -> None:
        tampered = measured_context(6_000)
        tampered["budget"]["included_bytes"] = 5_999
        with self.assertRaises(ContinuityEvaluationError) as caught:
            evaluate_pair(candidate_context=tampered)
        self.assertEqual("CONTEXT_HASH_MISMATCH", caught.exception.code)

    def test_report_is_deterministic_and_matches_the_strict_schema(self) -> None:
        first = evaluate_pair()
        second = evaluate_pair()
        self.assertEqual(first, second)

        schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema).iter_errors(first))
        self.assertEqual([], errors)


class PairedContextReductionRunnerTest(unittest.TestCase):
    def test_runner_publishes_immutable_paired_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shared-mind-paired-eval-") as raw:
            root = Path(raw)
            documents = {
                "baseline-context.json": measured_context(12_000),
                "baseline-observation.json": observation(),
                "candidate-context.json": measured_context(6_000),
                "candidate-observation.json": observation(),
                "expectation.json": expectation(),
                "thresholds.json": thresholds(),
            }
            for name, document in documents.items():
                (root / name).write_text(json.dumps(document), encoding="utf-8")
            output = root / "result.json"
            arguments = (
                root / "baseline-context.json",
                root / "baseline-observation.json",
                root / "candidate-context.json",
                root / "candidate-observation.json",
                root / "expectation.json",
                root / "thresholds.json",
                output,
            )
            keyword = {
                "baseline_elapsed_ms": 1_000,
                "candidate_elapsed_ms": 400,
                "baseline_token_count": 3_000,
                "candidate_token_count": 1_500,
            }

            first = run_paired_evaluation(*arguments, **keyword)
            before = output.read_bytes()
            second = run_paired_evaluation(*arguments, **keyword)
            self.assertEqual(first, second)
            self.assertEqual(before, output.read_bytes())
            self.assertEqual("paired-context-reduction-run@1", first["artifact_version"])
            self.assertTrue(first["passed"])

            changed = documents["candidate-observation.json"] | {
                "decision_ids": ["decision-wrong"]
            }
            (root / "candidate-observation.json").write_text(
                json.dumps(changed), encoding="utf-8"
            )
            with self.assertRaises(FileExistsError):
                run_paired_evaluation(*arguments, **keyword)


class PairedContextReductionInterfaceTest(ProductTestCase):
    def _cli(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        code = product_cli_main(
            ["--workspace", str(self.workspace_root), *args], stdout=output
        )
        return code, json.loads(output.getvalue())

    def test_product_service_translates_evaluation_errors(self) -> None:
        with self.assertRaises(ProductError) as caught:
            self.service.evaluate_paired_context_reduction(
                measured_context(12_000),
                observation(),
                measured_context(6_000, state_root="sha256:" + "4" * 64),
                observation(),
                expectation(),
                thresholds(),
                baseline_elapsed_ms=1_000,
                candidate_elapsed_ms=400,
                baseline_token_count=3_000,
                candidate_token_count=1_500,
            )
        self.assertEqual("PAIRED_STATE_ROOT_MISMATCH", caught.exception.code)

    def test_python_cli_and_mcp_return_the_same_paired_report(self) -> None:
        documents = {
            "baseline-context.json": measured_context(12_000),
            "baseline-observation.json": observation(),
            "candidate-context.json": measured_context(6_000),
            "candidate-observation.json": observation(),
            "expectation.json": expectation(),
            "thresholds.json": thresholds(),
        }
        for name, document in documents.items():
            self.write_source(f"eval/{name}", json.dumps(document))

        expected = self.service.evaluate_paired_context_reduction(
            documents["baseline-context.json"],
            documents["baseline-observation.json"],
            documents["candidate-context.json"],
            documents["candidate-observation.json"],
            documents["expectation.json"],
            documents["thresholds.json"],
            baseline_elapsed_ms=1_000,
            candidate_elapsed_ms=400,
            baseline_token_count=3_000,
            candidate_token_count=1_500,
        )
        code, cli = self._cli(
            "metrics",
            "context-reduction",
            "eval/baseline-context.json",
            "eval/baseline-observation.json",
            "eval/candidate-context.json",
            "eval/candidate-observation.json",
            "eval/expectation.json",
            "eval/thresholds.json",
            "--baseline-elapsed-ms",
            "1000",
            "--candidate-elapsed-ms",
            "400",
            "--baseline-token-count",
            "3000",
            "--candidate-token-count",
            "1500",
        )
        self.assertEqual(0, code)
        self.assertEqual("CONTEXT_REDUCTION_EVALUATED", cli["code"])
        self.assertEqual(expected, cli["data"])

        app = ProductMcpApplication(self.workspace)
        try:
            mcp = app.call_tool(
                "continuity_evaluate",
                {
                    "evaluation": "PAIRED_CONTEXT_REDUCTION",
                    "baseline_context": documents["baseline-context.json"],
                    "baseline_observation": documents["baseline-observation.json"],
                    "candidate_context": documents["candidate-context.json"],
                    "candidate_observation": documents["candidate-observation.json"],
                    "expectation": documents["expectation.json"],
                    "thresholds": documents["thresholds.json"],
                    "baseline_elapsed_ms": 1_000,
                    "candidate_elapsed_ms": 400,
                    "baseline_token_count": 3_000,
                    "candidate_token_count": 1_500,
                },
            )
        finally:
            app.close()
        self.assertFalse(mcp["isError"])
        self.assertEqual("CONTEXT_REDUCTION_EVALUATED", mcp["structuredContent"]["code"])
        self.assertEqual(expected, mcp["structuredContent"]["data"])


if __name__ == "__main__":
    unittest.main()
