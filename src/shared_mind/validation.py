from __future__ import annotations

import json
import sysconfig
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


SCHEMA_FILENAME = "shared-mind-kernel.schema.v1.json"


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
    Draft202012Validator.check_schema(contract)
    return Draft202012Validator(contract, format_checker=FormatChecker())
