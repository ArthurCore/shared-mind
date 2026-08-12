from __future__ import annotations

import importlib
import copy
import json
import socket
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "product_continuity"
SCENARIO_PATH = EVAL_ROOT / "golden-atlas-continuity.v1.json"
RESPONSE_SCHEMA_PATH = (
    EVAL_ROOT / "product-continuity-response.schema.v1.json"
)
METRICS_SCHEMA_PATH = EVAL_ROOT / "product-continuity-metrics.schema.v1.json"
REPORT_SCHEMA_PATH = EVAL_ROOT / "product-continuity-report.schema.v1.json"
LIVE_SUMMARY_SCHEMA_PATH = (
    EVAL_ROOT / "product-continuity-live-summary.schema.v1.json"
)


class ProductContinuityEvalContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = cls._load_json(SCENARIO_PATH)
        cls.response_schema = cls._load_json(RESPONSE_SCHEMA_PATH)
        cls.metrics_schema = cls._load_json(METRICS_SCHEMA_PATH)
        cls.report_schema = cls._load_json(REPORT_SCHEMA_PATH)
        cls.live_summary_schema = cls._load_json(LIVE_SUMMARY_SCHEMA_PATH)
        cls.response_validator = cls._validator(cls.response_schema)
        cls.metrics_validator = cls._validator(cls.metrics_schema)
        cls.report_validator = cls._validator(cls.report_schema)
        cls.live_summary_validator = cls._validator(cls.live_summary_schema)

    def test_fixture_schemas_and_every_candidate_response_are_valid(self) -> None:
        self.assertEqual(
            "product-continuity-scenario@1",
            self.scenario["scenario_version"],
        )
        self.assertEqual(RESPONSE_SCHEMA_PATH.name, self.scenario["response_schema"])
        self.assertEqual(METRICS_SCHEMA_PATH.name, self.scenario["metrics_schema"])
        self.assertEqual(REPORT_SCHEMA_PATH.name, self.scenario["report_schema"])

        for schema in (
            self.response_schema,
            self.metrics_schema,
            self.report_schema,
            self.live_summary_schema,
        ):
            Draft202012Validator.check_schema(schema)

        responses = [self.scenario["expected_response"]] + [
            case["response"] for case in self.scenario["adversarial_cases"]
        ]
        for response in responses:
            with self.subTest(response=response):
                self._assert_valid(self.response_validator, response)
        self._assert_valid(self.metrics_validator, self.scenario["metrics"])

    def test_sanitized_live_summary_schema_accepts_only_shareable_evidence(
        self,
    ) -> None:
        settings_pattern = self.live_summary_schema["properties"]["settings"][
            "propertyNames"
        ]["not"]["pattern"]
        self.assertNotIn(
            "(?i)",
            settings_pattern,
            "JSON Schema regexes must remain ECMAScript-compatible",
        )
        runner = importlib.import_module("evals.product_continuity.runner")
        report = runner.evaluate_scenario(
            self.scenario, self.scenario["expected_response"]
        )
        response_schema_bytes = RESPONSE_SCHEMA_PATH.read_bytes()
        summary = {
            "artifact_version": "product-continuity-live-summary@1",
            "scenario_id": self.scenario["scenario_id"],
            "project_snapshot_digest": "sha256:" + "a" * 64,
            "provider": "OpenAI/Codex",
            "model_snapshot": "gpt-5.5-codex-2026-08-11",
            "client": {
                "name": "openai-python",
                "version": "2.3.4",
            },
            "tokenizer": {
                "name": "o200k_base",
                "version": "2026.08.11",
            },
            "prompt_template_sha256": "sha256:" + "b" * 64,
            "response_schema_sha256": runner.sha256_bytes(response_schema_bytes),
            "settings": {
                "temperature": 0,
                "tools": "disabled",
                "web_search": "disabled",
            },
            "arms": {
                "manual_baseline": {
                    "input_bytes": 24000,
                    "input_tokens": 6000,
                    "elapsed_time_seconds": 120.0,
                    "schema_validation": "PASS",
                    "report": report,
                },
                "context_only": {
                    "input_bytes": 9720,
                    "input_tokens": 2430,
                    "elapsed_time_seconds": 45.0,
                    "schema_validation": "PASS",
                    "report": report,
                },
            },
            "redaction_attestation": {
                "reviewer": "local-owner-review",
                "reviewed_at": "2026-08-11T00:00:00Z",
                "statement": (
                    "Reviewed sanitized aggregate evidence; no secrets, "
                    "account identifiers, request IDs, absolute paths, raw "
                    "private source bytes, or unsanitized prompts/responses remain."
                ),
            },
        }
        summary["comparison"] = runner.live_summary_comparison(summary)

        self._assert_valid(self.live_summary_validator, summary)
        self.assertTrue(summary["comparison"]["passed"])

        floating_alias = dict(summary)
        floating_alias["model_snapshot"] = "latest"
        self.assertNotEqual(
            [],
            list(self.live_summary_validator.iter_errors(floating_alias)),
        )

        leaked_secret = dict(summary)
        leaked_secret["api_key"] = "sk-test-not-allowed"
        self.assertNotEqual(
            [],
            list(self.live_summary_validator.iter_errors(leaked_secret)),
        )
        nested_secret = copy.deepcopy(summary)
        nested_secret["settings"]["api_key"] = "sk-test-not-allowed"
        self.assertNotEqual(
            [],
            list(self.live_summary_validator.iter_errors(nested_secret)),
        )
        nested_alias_secret = copy.deepcopy(summary)
        nested_alias_secret["settings"]["API_KEY"] = "case-insensitive-block"
        self.assertNotEqual(
            [],
            list(self.live_summary_validator.iter_errors(nested_alias_secret)),
        )
        nested_path = copy.deepcopy(summary)
        nested_path["settings"]["request_id"] = "req_private_001"
        self.assertNotEqual(
            [],
            list(self.live_summary_validator.iter_errors(nested_path)),
        )
        leaked_report = copy.deepcopy(summary)
        leaked_report["arms"]["context_only"]["report"]["raw_prompt"] = (
            "private prompt must never be retained in a shareable summary"
        )
        self.assertNotEqual(
            [],
            list(self.live_summary_validator.iter_errors(leaked_report)),
        )

    def test_golden_response_is_fully_grounded_in_context_only_input(self) -> None:
        context = self.scenario["context"]
        response = self.scenario["expected_response"]
        self.assertIn("evaluation_scenario_id", context)
        self.assertEqual(
            self.scenario["scenario_id"],
            context["evaluation_scenario_id"],
        )
        self.assertEqual(context["evaluation_scenario_id"], response["scenario_id"])
        self.assertEqual(context["purpose"], response["project_purpose"])
        self.assertFalse(context["purpose_missing"])

        context_decisions = {
            item["document"]["decision_id"]: item["document"]
            for item in context["decisions"]
        }
        response_decisions = {
            item["decision_id"]: item for item in response["current_decisions"]
        }
        self.assertEqual(set(context_decisions), set(response_decisions))
        for decision_id, expected in context_decisions.items():
            actual = response_decisions[decision_id]
            self.assertEqual(expected["title"], actual["title"])
            self.assertEqual(expected["conclusion"], actual["conclusion"])
            self.assertEqual(expected["rationale"], actual["rationale"])

        context_claims = {
            item["claim_id"]: item for item in context["current_claims"]
        }
        response_claims = {
            item["claim_id"]: item for item in response["settled_claims"]
        }
        self.assertEqual(set(context_claims), set(response_claims))
        for claim_id, claim in context_claims.items():
            self.assertEqual(
                claim["proposition_hash"],
                response_claims[claim_id]["proposition_hash"],
            )
            expected_locators = {
                (
                    evidence["evidence_link_id"],
                    evidence["source_revision_id"],
                    evidence["selector"]["start_byte"],
                    evidence["selector"]["end_byte"],
                    evidence["selector"]["excerpt_hash"],
                )
                for evidence in claim["evidence"]
            }
            actual_locators = {
                (
                    locator["evidence_link_id"],
                    locator["source_revision_id"],
                    locator["start_byte"],
                    locator["end_byte"],
                    locator["excerpt_hash"],
                )
                for locator in response_claims[claim_id]["evidence_locators"]
            }
            self.assertEqual(expected_locators, actual_locators)

        context_conflicts = {
            conflict["conflict_id"]: {
                member["claim_id"] for member in conflict["members"]
            }
            for conflict in context["open_conflicts"]
        }
        response_conflicts = {
            conflict["conflict_id"]: {
                member["claim_id"] for member in conflict["member_claims"]
            }
            for conflict in response["open_conflicts"]
        }
        self.assertEqual(context_conflicts, response_conflicts)
        response_conflict_items = {
            conflict["conflict_id"]: {
                member["claim_id"]: member
                for member in conflict["member_claims"]
            }
            for conflict in response["open_conflicts"]
        }
        for conflict in context["open_conflicts"]:
            members = response_conflict_items[conflict["conflict_id"]]
            for member in conflict["members"]:
                actual = members[member["claim_id"]]
                self.assertEqual(member["proposition_hash"], actual["proposition_hash"])
                self.assertEqual(member["status"], actual["status"])
        conflict_member_ids = set().union(*context_conflicts.values())
        self.assertTrue(conflict_member_ids.isdisjoint(response_claims))

        self.assertEqual(
            {
                item["document"]["question_id"]
                for item in context["open_questions"]
            },
            {item["question_id"] for item in response["open_questions"]},
        )
        self.assertEqual(
            {
                item["document"]["work_item_id"] for item in context["work_items"]
            },
            {
                item["work_item_id"] for item in response["actionable_work_items"]
            },
        )

        weights = self.scenario["scoring"]["dimensions"]
        self.assertEqual(100, sum(weights.values()))
        self.assertEqual(100, self.scenario["scoring"]["passing_score"])

    def test_false_settled_and_hallucinated_id_cases_are_schema_valid_traps(
        self,
    ) -> None:
        cases = {
            item["expected_penalty_code"]: item["response"]
            for item in self.scenario["adversarial_cases"]
        }
        self.assertEqual(
            {"FALSE_SETTLED_CONFLICT_MEMBER", "HALLUCINATED_ID"}, set(cases)
        )

        conflict_member_ids = {
            member["claim_id"]
            for conflict in self.scenario["context"]["open_conflicts"]
            for member in conflict["members"]
        }
        false_settled_ids = {
            claim["claim_id"]
            for claim in cases["FALSE_SETTLED_CONFLICT_MEMBER"]["settled_claims"]
        }
        self.assertFalse(conflict_member_ids.isdisjoint(false_settled_ids))

        grounded_ids = self._object_ids(self.scenario["context"])
        response_ids = self._object_ids(cases["HALLUCINATED_ID"])
        self.assertIn("question_hallucinated_followup_001", response_ids - grounded_ids)

    def test_manual_baseline_reductions_are_at_least_fifty_percent(self) -> None:
        metrics = self.scenario["metrics"]
        minimum = metrics["minimum_reduction_fraction"]
        reductions = {
            name: 1 - metrics["context_only"][name] / metrics["manual_baseline"][name]
            for name in ("bytes", "tokens", "time_seconds")
        }
        self.assertEqual(0.5, minimum)
        for name, reduction in reductions.items():
            with self.subTest(metric=name):
                self.assertGreaterEqual(reduction, minimum)

        baseline_quality = metrics["quality"]["manual_baseline"]
        context_quality = metrics["quality"]["context_only"]
        for name, baseline in baseline_quality.items():
            with self.subTest(quality=name):
                self.assertGreaterEqual(context_quality[name], baseline)

    def test_live_client_is_explicitly_opt_in_and_network_is_forbidden_in_tests(
        self,
    ) -> None:
        policy = self.scenario["execution_policy"]
        self.assertEqual("OFFLINE_GOLDEN", policy["default_mode"])
        self.assertFalse(policy["network_allowed_in_tests"])
        self.assertFalse(policy["live_client"]["enabled_by_default"])
        self.assertTrue(policy["live_client"]["requires_explicit_opt_in"])
        self.assertEqual(
            "SHARED_MIND_PRODUCT_CONTINUITY_LIVE",
            policy["live_client"]["environment_variable"],
        )

    def test_eval_runner_scores_golden_and_penalizes_adversarial_responses(
        self,
    ) -> None:
        runner = importlib.import_module("evals.product_continuity.runner")
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network is forbidden in deterministic evals"),
        ):
            golden = runner.evaluate_scenario(
                self.scenario, self.scenario["expected_response"]
            )
            adversarial = [
                (
                    case["expected_penalty_code"],
                    runner.evaluate_scenario(self.scenario, case["response"]),
                )
                for case in self.scenario["adversarial_cases"]
            ]

        self._assert_valid(self.report_validator, golden)
        self.assertEqual(100, golden["score"])
        self.assertEqual(100, golden["maximum_score"])
        self.assertTrue(golden["passed"])
        self.assertEqual(1.0, golden["fact_accuracy"])
        self.assertEqual(1.0, golden["open_conflict_member_recall"])
        self.assertEqual([], golden["penalty_codes"])
        self.assertTrue(golden["metric_comparison"]["meets_reduction_target"])
        self.assertTrue(golden["metric_comparison"]["quality_preserved"])

        for expected_penalty, report in adversarial:
            with self.subTest(penalty=expected_penalty):
                self._assert_valid(self.report_validator, report)
                self.assertLess(report["score"], 100)
                self.assertFalse(report["passed"])
                self.assertIn(expected_penalty, report["penalty_codes"])

    def test_live_like_paraphrase_preserves_canonical_facts_without_exact_prose(
        self,
    ) -> None:
        runner = importlib.import_module("evals.product_continuity.runner")
        paraphrase = copy.deepcopy(self.scenario["expected_response"])
        paraphrase["settled_claims"][0]["summary"] = (
            "Backups for the Atlas production system have already been checked."
        )
        paraphrase["open_conflicts"][0]["member_claims"][0]["summary"] = (
            "One active claim says production is on MySQL."
        )
        paraphrase["open_conflicts"][0]["member_claims"][1]["summary"] = (
            "The competing active claim says production is on PostgreSQL."
        )

        report = runner.evaluate_scenario(self.scenario, paraphrase)

        self._assert_valid(self.report_validator, report)
        self.assertEqual(100, report["score"])
        self.assertTrue(report["passed"])
        self.assertEqual(1.0, report["fact_accuracy"])
        self.assertEqual(1.0, report["open_conflict_member_recall"])
        self.assertEqual([], report["penalty_codes"])

    def test_scorer_rejects_wrong_scenario_id(self) -> None:
        runner = importlib.import_module("evals.product_continuity.runner")
        response = copy.deepcopy(self.scenario["expected_response"])
        response["scenario_id"] = "atlas-continuity-wrong-v1"

        report = runner.evaluate_scenario(self.scenario, response)

        self._assert_valid(self.report_validator, report)
        self.assertLess(report["score"], 100)
        self.assertFalse(report["passed"])

    def test_response_schema_requires_canonical_claim_hashes_not_summary_only(
        self,
    ) -> None:
        response = copy.deepcopy(self.scenario["expected_response"])
        response["settled_claims"][0]["summary"] = (
            "Some unrelated prose that still should not carry canonical meaning."
        )
        del response["settled_claims"][0]["proposition_hash"]
        response["open_conflicts"][0]["member_claims"][0]["summary"] = (
            "Another unrelated sentence."
        )
        del response["open_conflicts"][0]["member_claims"][0]["proposition_hash"]

        self.assertNotEqual(
            [],
            list(self.response_validator.iter_errors(response)),
        )

    def test_scorer_rejects_duplicate_conflict_members(self) -> None:
        runner = importlib.import_module("evals.product_continuity.runner")
        response = copy.deepcopy(self.scenario["expected_response"])
        duplicate = copy.deepcopy(response["open_conflicts"][0]["member_claims"][0])
        duplicate["summary"] = "Duplicate member with different prose."
        response["open_conflicts"][0]["member_claims"].append(duplicate)

        report = runner.evaluate_scenario(self.scenario, response)

        self._assert_valid(self.report_validator, report)
        self.assertLess(report["score"], 100)
        self.assertFalse(report["passed"])

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise AssertionError(f"expected a JSON object: {path}")
        return value

    @staticmethod
    def _validator(schema: Mapping[str, Any]) -> Draft202012Validator:
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def _assert_valid(
        self, validator: Draft202012Validator, instance: Mapping[str, Any]
    ) -> None:
        errors = sorted(
            validator.iter_errors(instance),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        self.assertEqual([], [error.message for error in errors])

    @classmethod
    def _object_ids(cls, value: Any) -> set[str]:
        identifiers: set[str] = set()
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key != "scenario_id" and key.endswith("_id") and isinstance(item, str):
                    identifiers.add(item)
                identifiers.update(cls._object_ids(item))
        elif isinstance(value, list):
            for item in value:
                identifiers.update(cls._object_ids(item))
        return identifiers


if __name__ == "__main__":
    unittest.main()
