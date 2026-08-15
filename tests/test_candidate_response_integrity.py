from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from evals.product_continuity import runner


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = json.loads(
    (ROOT / "evals" / "product_continuity" / "golden-atlas-continuity.v1.json")
    .read_text(encoding="utf-8")
)


class CandidateResponseIntegrityTest(unittest.TestCase):
    def assert_invalid(self, response: Any) -> None:
        with self.assertRaisesRegex(ValueError, "INVALID_CANDIDATE_RESPONSE"):
            runner.evaluate_scenario(SCENARIO, response)

    def test_non_mapping_and_missing_fields_fail_closed(self) -> None:
        self.assert_invalid([])
        for field in SCENARIO["expected_response"]:
            with self.subTest(field=field):
                response = copy.deepcopy(SCENARIO["expected_response"])
                response.pop(field)
                self.assert_invalid(response)

    def test_unknown_top_level_fields_cannot_hide_private_content(self) -> None:
        for field in ("raw_prompt", "api_key", "private_note"):
            with self.subTest(field=field):
                response = copy.deepcopy(SCENARIO["expected_response"])
                response[field] = "must not be accepted"
                self.assert_invalid(response)

    def test_unknown_nested_fields_fail_closed(self) -> None:
        cases = (
            lambda response: response["current_decisions"][0].__setitem__("private_note", "x"),
            lambda response: response["settled_claims"][0].__setitem__("confidence", 1.0),
            lambda response: response["settled_claims"][0]["evidence_locators"][0].__setitem__("raw_source", "x"),
            lambda response: response["open_conflicts"][0].__setitem__("resolution", "invented"),
            lambda response: response["open_conflicts"][0]["member_claims"][0].__setitem__("confidence", 1.0),
            lambda response: response["open_questions"][0].__setitem__("answer", "invented"),
            lambda response: response["actionable_work_items"][0].__setitem__("owner_secret", "x"),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                response = copy.deepcopy(SCENARIO["expected_response"])
                mutate(response)
                self.assert_invalid(response)

    def test_invalid_ids_hashes_statuses_and_bounds_fail_closed(self) -> None:
        cases = (
            lambda response: response.__setitem__("scenario_id", "INVALID ID"),
            lambda response: response["settled_claims"][0].__setitem__("proposition_hash", "sha256:bad"),
            lambda response: response["settled_claims"][0].__setitem__("summary", ""),
            lambda response: response["settled_claims"][0]["evidence_locators"][0].__setitem__("start_byte", -1),
            lambda response: response["open_conflicts"][0].__setitem__("status", "RESOLVED"),
            lambda response: response["open_conflicts"][0].__setitem__("member_claims", []),
            lambda response: response["actionable_work_items"][0].__setitem__("status", "DONE"),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                response = copy.deepcopy(SCENARIO["expected_response"])
                mutate(response)
                self.assert_invalid(response)

    def test_expected_response_uses_the_same_closed_schema(self) -> None:
        scenario = copy.deepcopy(SCENARIO)
        scenario["expected_response"]["settled_claims"][0]["confidence"] = 1.0

        with self.assertRaisesRegex(ValueError, "INVALID_SCENARIO_CONTRACT"):
            runner.evaluate_scenario(scenario, scenario["expected_response"])

    def test_valid_and_paraphrased_responses_remain_supported(self) -> None:
        valid = runner.evaluate_scenario(SCENARIO, SCENARIO["expected_response"])
        paraphrase = copy.deepcopy(SCENARIO["expected_response"])
        paraphrase["settled_claims"][0]["summary"] = "Backup verification remains grounded."
        paraphrase["open_conflicts"][0]["member_claims"][0]["summary"] = "MySQL remains an active conflicting claim."
        paraphrased = runner.evaluate_scenario(SCENARIO, paraphrase)

        self.assertTrue(valid["passed"])
        self.assertTrue(paraphrased["passed"])
        self.assertEqual(100, paraphrased["score"])


if __name__ == "__main__":
    unittest.main()
