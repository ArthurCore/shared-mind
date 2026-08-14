"""Validation helpers for product-layer contracts."""

from __future__ import annotations

import json
import sysconfig
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


PRODUCT_SCHEMA_FILENAME = "shared-mind-product.schema.v1.json"


def _contract_candidates() -> tuple[Path, ...]:
    module_root = Path(__file__).resolve().parents[2]
    installed_root = Path(sysconfig.get_path("data")) / "share" / "shared-mind" / "contracts"
    return (
        module_root / "contracts" / PRODUCT_SCHEMA_FILENAME,
        installed_root / PRODUCT_SCHEMA_FILENAME,
    )


@lru_cache(maxsize=1)
def load_product_schema() -> dict[str, Any]:
    for path in _contract_candidates():
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"Cannot locate {PRODUCT_SCHEMA_FILENAME}")


@lru_cache(maxsize=None)
def validator_for(definition: str | None = None) -> Draft202012Validator:
    schema = load_product_schema()
    if definition is None:
        target = schema
    else:
        if definition not in schema["$defs"]:
            raise KeyError(definition)
        target = {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{definition}",
        }
    Draft202012Validator.check_schema(target)
    return Draft202012Validator(target, format_checker=FormatChecker())


def validate_product_object(value: Any, definition: str | None = None) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for error in sorted(
        validator_for(definition).iter_errors(value),
        key=lambda item: (tuple(str(part) for part in item.absolute_path), item.message),
    ):
        path = "$"
        for part in error.absolute_path:
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        issues.append(
            {
                "code": "PRODUCT_SCHEMA_VALIDATION_FAILED",
                "object_path": path,
                "message": error.message,
            }
        )
    return issues


__all__ = [
    "PRODUCT_SCHEMA_FILENAME",
    "load_product_schema",
    "validate_product_object",
    "validator_for",
]
