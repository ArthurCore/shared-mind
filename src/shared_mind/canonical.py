from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Stable JSON encoding used by the v1 prototype.

    The contract calls for RFC 8785. For v1 data (strings, integers, booleans,
    null, arrays and objects) this compact sorted representation is sufficient.
    Floating point values are intentionally not accepted by the domain schema.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()

