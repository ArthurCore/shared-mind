from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from shared_mind.memory_views import MemoryViewError, normalize_context_request
from shared_mind.product_contract import load_product_schema, validate_product_object


ROOT = Path(__file__).resolve().parents[1]


class ProductContractTest(unittest.TestCase):
    def test_schema_and_checked_in_fixtures_validate(self) -> None:
        schema = load_product_schema()
        Draft202012Validator.check_schema(schema)
        fixtures = json.loads(
            (ROOT / "contracts/product-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        by_name = {item["name"]: item["object"] for item in fixtures["typed_objects"]}
        validator = Draft202012Validator(schema)
        for name, document in by_name.items():
            self.assertTrue(validator.is_valid(document), name)
        for case in fixtures["negative_schema_cases"]:
            candidate = copy.deepcopy(by_name[case["base_object"]])
            for field in case["remove_fields"]:
                candidate.pop(field, None)
            candidate.update(copy.deepcopy(case["replace_fields"]))
            self.assertFalse(validator.is_valid(candidate), case["name"])

    def test_contract_validator_script(self) -> None:
        result = subprocess.run(
            [sys.executable, "contracts/validate_product_contract.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("8 typed fixtures", result.stdout)

    def test_context_request_forbids_agent_partition_hints(self) -> None:
        with self.assertRaises(MemoryViewError) as caught:
            normalize_context_request(
                {"task": "review", "hints": {"agent_id": "agent:reviewer"}}
            )
        self.assertEqual("AGENT_PARTITION_HINT_FORBIDDEN", caught.exception.code)

    def test_validation_errors_are_stable_and_path_aware(self) -> None:
        issues = validate_product_object(
            {
                "object_type": "CONTEXT_REQUEST",
                "request_version": "context-request@1",
                "task": "",
                "purpose": None,
                "query": None,
                "references": [],
                "depth": "DETAIL",
                "budget_bytes": 4096,
                "budget_tokens": None,
                "hints": {},
            },
            "ContextRequest",
        )
        self.assertEqual("PRODUCT_SCHEMA_VALIDATION_FAILED", issues[0]["code"])
        self.assertEqual("$.task", issues[0]["object_path"])


if __name__ == "__main__":
    unittest.main()
