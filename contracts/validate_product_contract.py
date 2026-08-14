#!/usr/bin/env python3
"""Validate Shared Mind product contracts and conformance fixtures."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent


def load(name: str) -> object:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def main() -> None:
    schema = load("shared-mind-product.schema.v1.json")
    fixtures = load("product-conformance-fixtures.v1.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    typed = fixtures["typed_objects"]
    by_name = {item["name"]: item["object"] for item in typed}
    if len(by_name) != len(typed):
        raise ValueError("Product fixture names must be unique")
    for fixture in typed:
        errors = sorted(validator.iter_errors(fixture["object"]), key=lambda item: list(item.path))
        if errors:
            raise ValueError(f"Positive fixture failed {fixture['name']}: {errors[0].message}")
    negatives = fixtures.get("negative_schema_cases", [])
    for case in negatives:
        candidate = copy.deepcopy(by_name[case["base_object"]])
        for field in case.get("remove_fields", []):
            candidate.pop(field, None)
        candidate.update(copy.deepcopy(case.get("replace_fields", {})))
        if validator.is_valid(candidate):
            raise ValueError(f"Negative product fixture unexpectedly passed: {case['name']}")
    object_types = [item["object"].get("object_type") for item in typed]
    if len(object_types) != len(set(object_types)):
        raise ValueError("Positive fixtures must cover distinct public product object types")
    print(
        f"OK (Draft 2020-12 product): {len(typed)} typed fixtures + "
        f"{len(negatives)} negative cases"
    )


if __name__ == "__main__":
    main()
