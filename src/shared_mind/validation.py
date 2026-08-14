from __future__ import annotations

import json
import sysconfig
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


SCHEMA_FILENAME = "shared-mind-kernel.schema.v1.json"


@lru_cache(maxsize=16)
def _check_schema_document(serialized: str) -> None:
    """Check each distinct schema document once per process.

    Kernel instances are intentionally short-lived, but Draft 2020-12 meta-
    schema validation is expensive.  The result depends only on the canonical
    schema bytes, so caching the successful check preserves validation
    semantics while avoiding repeated multi-second walks of the same contract.
    """

    Draft202012Validator.check_schema(json.loads(serialized))


def _ensure_schema_checked(contract: dict[str, Any]) -> None:
    _check_schema_document(
        json.dumps(
            contract,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def load_default_schema() -> dict[str, Any]:
    """Load the v1 contract when running from a source checkout.

    Installed callers may pass an explicit schema to ``Kernel``. Keeping that
    injection point avoids silently running without validation when the
    repository-level contract is not present in a built distribution.
    """

    candidates = (
        Path(__file__).resolve().parents[2] / "contracts" / SCHEMA_FILENAME,
        Path(sysconfig.get_path("data"))
        / "share"
        / "shared-mind"
        / "contracts"
        / SCHEMA_FILENAME,
    )
    for schema_path in candidates:
        if schema_path.is_file():
            with schema_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
    locations = ", ".join(str(path) for path in candidates)
    raise RuntimeError(
        f"Shared Mind contract schema not found in: {locations}; pass schema= explicitly"
    )


def build_contract_validator(
    schema: dict[str, Any] | None = None,
) -> Draft202012Validator:
    contract = schema if schema is not None else load_default_schema()
    _ensure_schema_checked(contract)
    return Draft202012Validator(contract, format_checker=FormatChecker())


def build_definition_validator(
    definition: str,
    schema: dict[str, Any] | None = None,
) -> Draft202012Validator:
    """Build a validator for one named contract definition."""

    contract = schema if schema is not None else load_default_schema()
    _ensure_schema_checked(contract)
    if definition not in contract.get("$defs", {}):
        raise KeyError(f"Unknown contract definition: {definition}")
    focused = {
        "$schema": contract["$schema"],
        "$defs": contract["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    return Draft202012Validator(focused, format_checker=FormatChecker())
