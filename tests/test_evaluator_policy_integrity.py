from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from evals.product_continuity import runner


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = json.loads(
    (
        ROOT
        / "evals"
        / "product_continuity"
        / "golden-atlas-continuity.v1.json"
    ).read_text(encoding="utf-8")
)


class EvaluatorPolicyIntegrityTest(unittest.TestCase):
    def assert_invalid(self, scenario: object) -> str:
        response = (
            scenario.get("expected_response")
            if isinstance(scenario, dict)
            else SCENARIO["expected_response"]
        )
        with self.assertRaisesRegex(
            ValueError, "INVALID_SCENARIO_CONTRACT"
        ) as raised:
            runner.evaluate_scenario(scenario, response)  # type: ignore[arg-type]
        return str(raised.exception)

    def test_execution_policy_uses_the_exact_offline_fail_closed_contract(self) -> None:
        policy = SCENARIO["execution_policy"]
        self.assertEqual(
            {
                "default_mode": "OFFLINE_GOLDEN",
                "network_allowed_in_tests": False,
                "live_client": {
                    "enabled_by_default": False,
                    "requires_explicit_opt_in": True,
                    "environment_variable": "SHARED_MIND_PRODUCT_CONTINUITY_LIVE",
                },
            },
            policy,
        )

        invalid_policies = (
            {},
            {**policy, "api_key": "sk-private"},
            {**policy, "default_mode": "LIVE"},
            {**policy, "network_allowed_in_tests": True},
            {**policy, "network_allowed_in_tests": 0},
            {**policy, "live_client": {}},
            {
                **policy,
                "live_client": {
                    **policy["live_client"],
                    "enabled_by_default": True,
                },
            },
            {
                **policy,
                "live_client": {
                    **policy["live_client"],
                    "environment_variable": "UNPINNED_LIVE_FLAG",
                },
            },
        )
        for invalid in invalid_policies:
            with self.subTest(invalid=invalid):
                scenario = copy.deepcopy(SCENARIO)
                scenario["execution_policy"] = invalid
                self.assert_invalid(scenario)

    def test_adversarial_cases_are_nonempty_closed_records(self) -> None:
        for invalid in (None, {}, [], ["case"]):
            with self.subTest(invalid=invalid):
                scenario = copy.deepcopy(SCENARIO)
                scenario["adversarial_cases"] = invalid
                self.assert_invalid(scenario)

        for mutation in ("missing", "extra"):
            with self.subTest(mutation=mutation):
                scenario = copy.deepcopy(SCENARIO)
                case = scenario["adversarial_cases"][0]
                if mutation == "missing":
                    case.pop("response")
                else:
                    case["raw_prompt"] = "private"
                self.assert_invalid(scenario)

    def test_adversarial_names_and_penalty_codes_are_unique_and_known(self) -> None:
        scenario = copy.deepcopy(SCENARIO)
        scenario["adversarial_cases"][1]["name"] = scenario[
            "adversarial_cases"
        ][0]["name"]
        self.assert_invalid(scenario)

        scenario = copy.deepcopy(SCENARIO)
        scenario["adversarial_cases"][1]["expected_penalty_code"] = scenario[
            "adversarial_cases"
        ][0]["expected_penalty_code"]
        self.assert_invalid(scenario)

        for invalid in ("Bad Name", "", 123):
            with self.subTest(name=invalid):
                scenario = copy.deepcopy(SCENARIO)
                scenario["adversarial_cases"][0]["name"] = invalid
                self.assert_invalid(scenario)

        scenario = copy.deepcopy(SCENARIO)
        scenario["adversarial_cases"][0]["expected_penalty_code"] = (
            "UNKNOWN_PENALTY"
        )
        self.assert_invalid(scenario)

    def test_adversarial_responses_use_the_closed_candidate_schema(self) -> None:
        cases = []
        extra = copy.deepcopy(SCENARIO)
        extra["adversarial_cases"][0]["response"]["raw_prompt"] = "private"
        cases.append(extra)

        missing = copy.deepcopy(SCENARIO)
        missing["adversarial_cases"][0]["response"].pop("open_questions")
        cases.append(missing)

        wrong_scenario = copy.deepcopy(SCENARIO)
        wrong_scenario["adversarial_cases"][0]["response"]["scenario_id"] = (
            "other-scenario-v1"
        )
        cases.append(wrong_scenario)

        for index, scenario in enumerate(cases):
            with self.subTest(index=index):
                self.assert_invalid(scenario)

    def test_declared_penalty_must_match_the_effective_adversarial_result(self) -> None:
        scenario = copy.deepcopy(SCENARIO)
        scenario["adversarial_cases"][0]["expected_penalty_code"] = (
            "HALLUCINATED_ID"
        )
        scenario["adversarial_cases"][1]["expected_penalty_code"] = (
            "FALSE_SETTLED_CONFLICT_MEMBER"
        )
        self.assert_invalid(scenario)

        ineffective = copy.deepcopy(SCENARIO)
        ineffective["adversarial_cases"][0]["response"] = copy.deepcopy(
            ineffective["expected_response"]
        )
        self.assert_invalid(ineffective)

    def test_each_golden_adversarial_case_triggers_exactly_its_declared_code(self) -> None:
        for case in SCENARIO["adversarial_cases"]:
            with self.subTest(name=case["name"]):
                report = runner.evaluate_scenario(SCENARIO, case["response"])
                self.assertEqual(
                    [case["expected_penalty_code"]], report["penalty_codes"]
                )

    def test_valid_golden_report_remains_exact(self) -> None:
        report = runner.evaluate_scenario(SCENARIO, SCENARIO["expected_response"])
        self.assertEqual(100, report["score"])
        self.assertTrue(report["passed"])
        self.assertEqual([], report["penalty_codes"])


if __name__ == "__main__":
    unittest.main()
