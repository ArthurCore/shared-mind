"""Pure, deterministic authorization policy for future remote adapters.

This module evaluates already-authenticated identity bindings.  It performs no
authentication, transport, file, environment, or network work; adapters must
provide the authenticated binding out of band from the untrusted request.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit


REASON_CODES = (
    "ACTOR_BINDING_MISMATCH",
    "DISCLOSURE_FIELD_DENIED",
    "ENDPOINT_PIN_MISMATCH",
    "MISSING_TRUST_BINDING",
    "OPERATION_SCOPE_DENIED",
    "REGISTRY_VERSION_MISMATCH",
    "REMOTE_REQUEST_VERSION_MISMATCH",
    "REMOTE_VERSION_PIN_MISMATCH",
    "SENSITIVITY_DENIED",
    "SOURCE_SCOPE_DENIED",
    "TRUST_BINDING_NOT_ALLOWED",
    "UNKNOWN_CAPABILITY",
)

_TRUST_FIELDS = ("binding_id", "issuer", "subject", "actor_id")
_REQUEST_VERSION = "remote-policy-request@1"


@dataclass(frozen=True)
class CompiledRemotePolicy:
    """An immutable handle to one canonicalized remote policy document."""

    policy_hash: str
    _document: Mapping[str, Any] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_document", _deep_freeze(self._document))


@dataclass(frozen=True)
class RemotePolicyDecision:
    """Canonical audit decision with a defensive JSON-compatible projection."""

    _document: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_document", _deep_freeze(self._document))

    def as_dict(self) -> dict[str, Any]:
        """Return an independent copy of the canonical audit document."""

        document = _thaw_json(self._document)
        _verify_decision_integrity(document)
        return _json_copy(document)


def compile_policy(document: Mapping[str, Any]) -> CompiledRemotePolicy:
    """Canonicalize and compile a deny-by-default remote policy."""

    if not isinstance(document, Mapping):
        raise TypeError("remote policy document must be a mapping")
    canonical_document = _strict_json_copy(document)
    _validate_policy(canonical_document)
    return CompiledRemotePolicy(
        policy_hash=_canonical_hash(canonical_document),
        _document=canonical_document,
    )


def evaluate_request(
    policy: CompiledRemotePolicy,
    request: Mapping[str, Any],
    *,
    authenticated_binding: Mapping[str, Any] | None,
) -> RemotePolicyDecision:
    """Evaluate a request without transport access or secret-bearing output."""

    if not isinstance(policy, CompiledRemotePolicy):
        raise TypeError("policy must be a CompiledRemotePolicy")
    if not isinstance(request, Mapping):
        raise TypeError("remote policy request must be a mapping")

    document = _thaw_json(policy._document)
    try:
        integrity_matches = _canonical_hash(document) == policy.policy_hash
    except (TypeError, ValueError):
        integrity_matches = False
    if not integrity_matches:
        raise ValueError("compiled remote policy integrity check failed")
    endpoint_pin = _required_mapping(document, "endpoint_pin")
    trusted_binding = _trusted_binding(document, authenticated_binding)
    authenticated_actor_id = _string_or_none(
        trusted_binding.get("actor_id") if trusted_binding else None
    )
    trust_binding_id = _string_or_none(
        trusted_binding.get("binding_id") if trusted_binding else None
    )

    capability = _string_or_none(request.get("capability"))
    operation_type = _string_or_none(request.get("operation_type"))
    scope = _capability_scope(document, capability)
    source_ref = _string_or_none(request.get("source_ref"))
    source_label = _source_label(document, source_ref)

    reason_code = _denial_reason(
        document=document,
        endpoint_pin=endpoint_pin,
        request=request,
        authenticated_binding=authenticated_binding,
        trusted_binding=trusted_binding,
        scope=scope,
        source_ref=source_ref,
        source_label=source_label,
    )
    outcome = "DENY" if reason_code else "ALLOW"
    disclosure = (
        _allowed_disclosure(scope, request)
        if outcome == "ALLOW"
        else {"allowed_fields": [], "redacted_paths": []}
    )

    audit_document: dict[str, Any] = {
        "decision_version": "remote-policy-decision@1",
        "request_id": _string_or_none(request.get("request_id")),
        "outcome": outcome,
        "reason_codes": [] if reason_code is None else [reason_code],
        "policy_hash": policy.policy_hash,
        "authenticated_actor_id": authenticated_actor_id,
        "trust_binding_id": trust_binding_id,
        "endpoint_pin": {
            "endpoint_id": _string_or_none(endpoint_pin.get("endpoint_id")),
            "protocol_version": _string_or_none(
                endpoint_pin.get("protocol_version")
            ),
            "adapter_version": _string_or_none(endpoint_pin.get("adapter_version")),
        },
        "registry_version": _string_or_none(document.get("registry_version")),
        "capability": capability,
        "operation_type": operation_type,
        "source_label": source_label,
        "disclosure": disclosure,
    }
    decision_digest = hashlib.sha256(
        _canonical_json(audit_document).encode("utf-8")
    ).hexdigest()
    audit_document["decision_id"] = f"remote_policy_decision_{decision_digest[:32]}"
    return RemotePolicyDecision(_document=audit_document)


def _denial_reason(
    *,
    document: Mapping[str, Any],
    endpoint_pin: Mapping[str, Any],
    request: Mapping[str, Any],
    authenticated_binding: Mapping[str, Any] | None,
    trusted_binding: Mapping[str, Any] | None,
    scope: Mapping[str, Any] | None,
    source_ref: str | None,
    source_label: Mapping[str, Any],
) -> str | None:
    if request.get("request_version") != _REQUEST_VERSION:
        return "REMOTE_REQUEST_VERSION_MISMATCH"
    if authenticated_binding is None:
        return "MISSING_TRUST_BINDING"
    if trusted_binding is None:
        return "TRUST_BINDING_NOT_ALLOWED"
    if request.get("claimed_actor_id") != trusted_binding.get("actor_id"):
        return "ACTOR_BINDING_MISMATCH"
    if request.get("endpoint_id") != endpoint_pin.get("endpoint_id"):
        return "ENDPOINT_PIN_MISMATCH"
    if (
        request.get("protocol_version") != endpoint_pin.get("protocol_version")
        or request.get("adapter_version") != endpoint_pin.get("adapter_version")
    ):
        return "REMOTE_VERSION_PIN_MISMATCH"
    if request.get("registry_version") != document.get("registry_version"):
        return "REGISTRY_VERSION_MISMATCH"
    if scope is None:
        return "UNKNOWN_CAPABILITY"
    if request.get("operation_type") not in _string_values(
        scope.get("operation_types")
    ):
        return "OPERATION_SCOPE_DENIED"
    if trusted_binding.get("actor_id") not in _string_values(scope.get("actor_ids")):
        return "ACTOR_BINDING_MISMATCH"
    if not _source_is_allowed(scope, source_ref) or source_label["source_root"] is None:
        return "SOURCE_SCOPE_DENIED"
    if source_label["sensitivity"] not in _string_values(
        scope.get("allowed_sensitivities")
    ):
        return "SENSITIVITY_DENIED"
    if not _disclosure_is_allowed(scope, request):
        return "DISCLOSURE_FIELD_DENIED"
    return None


def _trusted_binding(
    document: Mapping[str, Any],
    authenticated_binding: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if not isinstance(authenticated_binding, Mapping):
        return None
    if not all(
        _is_non_empty_string(authenticated_binding.get(field_name))
        for field_name in _TRUST_FIELDS
    ):
        return None
    for candidate in _mapping_values(document.get("trust_bindings")):
        if all(
            authenticated_binding.get(field_name) == candidate.get(field_name)
            for field_name in _TRUST_FIELDS
        ):
            return candidate
    return None


def _capability_scope(
    document: Mapping[str, Any], capability: str | None
) -> Mapping[str, Any] | None:
    for scope in _mapping_values(document.get("capability_scopes")):
        if scope.get("capability") == capability:
            return scope
    return None


def _source_label(
    document: Mapping[str, Any], source_ref: str | None
) -> dict[str, Any]:
    if source_ref is None or not _source_ref_is_safe(source_ref):
        return {"source_root": None, "sensitivity": None, "data_classes": []}
    matches = [
        label
        for label in _mapping_values(document.get("source_labels"))
        if isinstance(label.get("source_root"), str)
        and source_ref.startswith(label["source_root"])
    ]
    if not matches:
        return {"source_root": None, "sensitivity": None, "data_classes": []}
    selected = max(matches, key=lambda item: len(str(item["source_root"])))
    return {
        "source_root": selected["source_root"],
        "sensitivity": _string_or_none(selected.get("sensitivity")),
        "data_classes": sorted(_string_values(selected.get("data_classes"))),
    }


def _source_is_allowed(scope: Mapping[str, Any], source_ref: str | None) -> bool:
    return source_ref is not None and _source_ref_is_safe(source_ref) and any(
        source_ref.startswith(source_root)
        for source_root in _string_values(scope.get("source_roots"))
    )


def _source_ref_is_safe(source_ref: str) -> bool:
    normalized = source_ref
    for _ in range(8):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    else:
        # Excessive encoding is ambiguous and must not consume unbounded work.
        return False
    if "\\" in normalized:
        return False
    path_part = normalized.split("://", 1)[1] if "://" in normalized else normalized
    return all(segment not in (".", "..") for segment in path_part.split("/"))


def _disclosure_is_allowed(
    scope: Mapping[str, Any], request: Mapping[str, Any]
) -> bool:
    disclosure = _mapping_or_empty(scope.get("disclosure"))
    requested_fields = _string_values(request.get("requested_fields"))
    allowed_fields = set(_string_values(disclosure.get("allow_fields")))
    return bool(requested_fields) and set(requested_fields).issubset(allowed_fields)


def _allowed_disclosure(
    scope: Mapping[str, Any] | None, request: Mapping[str, Any]
) -> dict[str, list[str]]:
    if scope is None:
        return {"allowed_fields": [], "redacted_paths": []}
    disclosure = _mapping_or_empty(scope.get("disclosure"))
    requested_fields = sorted(set(_string_values(request.get("requested_fields"))))
    requested_roots = set(requested_fields)
    redacted_paths = sorted(
        path
        for path in _string_values(disclosure.get("redact_paths"))
        if path.partition(".")[0] in requested_roots
    )
    return {
        "allowed_fields": requested_fields,
        "redacted_paths": redacted_paths,
    }


def _validate_policy(document: Mapping[str, Any]) -> None:
    if document.get("policy_version") != "remote-adapter-policy@1":
        raise ValueError("unsupported remote policy version")
    if document.get("default_effect") != "DENY":
        raise ValueError("remote policies must deny by default")
    _require_non_empty_string(document, "policy_id", "remote policy")
    _require_non_empty_string(document, "registry_version", "remote policy")

    endpoint_pin = _required_mapping(document, "endpoint_pin")
    for field_name in (
        "endpoint_id",
        "origin",
        "protocol_version",
        "adapter_version",
    ):
        _require_non_empty_string(
            endpoint_pin, field_name, "remote policy endpoint_pin"
        )

    trust_bindings = _required_object_list(
        document, "trust_bindings", "remote policy"
    )
    binding_ids: list[str] = []
    trusted_actor_ids: set[str] = set()
    for index, binding in enumerate(trust_bindings):
        context = f"remote policy trust_bindings[{index}]"
        for field_name in _TRUST_FIELDS:
            value = _require_non_empty_string(binding, field_name, context)
            if field_name == "binding_id":
                binding_ids.append(value)
            elif field_name == "actor_id":
                trusted_actor_ids.add(value)
    _reject_duplicates(binding_ids, "remote policy trust_bindings binding_id")

    source_labels = _required_object_list(document, "source_labels", "remote policy")
    source_roots: list[str] = []
    sensitivities: set[str] = set()
    for index, label in enumerate(source_labels):
        context = f"remote policy source_labels[{index}]"
        source_root = _require_non_empty_string(label, "source_root", context)
        _validate_source_root(source_root, f"{context}.source_root")
        source_roots.append(source_root)
        sensitivities.add(_require_non_empty_string(label, "sensitivity", context))
        _required_string_list(
            label, "data_classes", context, allow_empty=True
        )
    _reject_duplicates(source_roots, "remote policy source_labels source_root")
    known_source_roots = set(source_roots)

    capability_scopes = _required_object_list(
        document, "capability_scopes", "remote policy"
    )
    capability_names: list[str] = []
    for index, scope in enumerate(capability_scopes):
        context = f"remote policy capability_scopes[{index}]"
        capability_names.append(
            _require_non_empty_string(scope, "capability", context)
        )
        _required_string_list(scope, "operation_types", context)
        actor_ids = _required_string_list(scope, "actor_ids", context)
        if not set(actor_ids).issubset(trusted_actor_ids):
            raise ValueError(f"{context}.actor_ids contains an untrusted actor")

        scoped_roots = _required_string_list(scope, "source_roots", context)
        for root_index, source_root in enumerate(scoped_roots):
            _validate_source_root(
                source_root, f"{context}.source_roots[{root_index}]"
            )
            if source_root not in known_source_roots:
                raise ValueError(
                    f"{context}.source_roots[{root_index}] has no source label"
                )

        allowed_sensitivities = _required_string_list(
            scope, "allowed_sensitivities", context
        )
        if not set(allowed_sensitivities).issubset(sensitivities):
            raise ValueError(
                f"{context}.allowed_sensitivities contains an unknown sensitivity"
            )

        disclosure = _required_mapping(scope, "disclosure", context=context)
        allow_fields = _required_string_list(
            disclosure, "allow_fields", f"{context}.disclosure"
        )
        if any("." in field_name for field_name in allow_fields):
            raise ValueError(
                f"{context}.disclosure.allow_fields must name top-level fields"
            )
        redact_paths = _required_string_list(
            disclosure,
            "redact_paths",
            f"{context}.disclosure",
            allow_empty=True,
        )
        _validate_redaction_paths(
            redact_paths,
            allowed_roots=set(allow_fields),
            context=f"{context}.disclosure.redact_paths",
        )
    _reject_duplicates(
        capability_names, "remote policy capability_scopes capability"
    )


def _required_mapping(
    document: Mapping[str, Any],
    field_name: str,
    *,
    context: str = "remote policy",
) -> Mapping[str, Any]:
    value = document.get(field_name)
    if not isinstance(value, Mapping):
        raise ValueError(f"{context}.{field_name} must be an object")
    return value


def _required_object_list(
    document: Mapping[str, Any], field_name: str, context: str
) -> list[Mapping[str, Any]]:
    value = document.get(field_name)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{context}.{field_name} must be a non-empty list")
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{context}.{field_name}[{index}] must be an object")
    return value


def _require_non_empty_string(
    document: Mapping[str, Any], field_name: str, context: str
) -> str:
    value = document.get(field_name)
    if not _is_non_empty_string(value):
        raise ValueError(f"{context}.{field_name} must be a non-empty string")
    return value


def _required_string_list(
    document: Mapping[str, Any],
    field_name: str,
    context: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    value = document.get(field_name)
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ValueError(f"{context}.{field_name} must be {qualifier}")
    for index, item in enumerate(value):
        if not _is_non_empty_string(item):
            raise ValueError(
                f"{context}.{field_name}[{index}] must be a non-empty string"
            )
    _reject_duplicates(value, f"{context}.{field_name}")
    return value


def _reject_duplicates(values: list[str], context: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{context} contains duplicate values")
        seen.add(value)


def _validate_source_root(source_root: str, context: str) -> None:
    parsed = urlsplit(source_root)
    if (
        not parsed.scheme
        or not parsed.netloc
        or not parsed.path.startswith("/")
        or not parsed.path.endswith("/")
        or parsed.query
        or parsed.fragment
        or not _source_ref_is_safe(source_root)
    ):
        raise ValueError(
            f"{context} must be an absolute, slash-terminated safe source root"
        )


def _validate_redaction_paths(
    paths: list[str], *, allowed_roots: set[str], context: str
) -> None:
    for index, path in enumerate(paths):
        segments = path.split(".")
        if len(segments) < 2 or any(not segment for segment in segments):
            raise ValueError(f"{context}[{index}] must be a nested dotted path")
        if segments[0] not in allowed_roots:
            raise ValueError(f"{context}[{index}] is not rooted in an allowed field")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path.startswith(f"{other}.") or other.startswith(f"{path}."):
                raise ValueError(f"{context} contains overlapping paths")


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_values(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_hash(value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _verify_decision_integrity(document: Mapping[str, Any]) -> None:
    decision_id = document.get("decision_id")
    payload = dict(document)
    payload.pop("decision_id", None)
    try:
        digest = hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        raise ValueError("remote policy decision integrity check failed") from None
    expected = f"remote_policy_decision_{digest[:32]}"
    if decision_id != expected:
        raise ValueError("remote policy decision integrity check failed")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _strict_json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("remote policy JSON object keys must be strings")
            copied[key] = _strict_json_copy(item)
        return copied
    if isinstance(value, list):
        return [_strict_json_copy(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("remote policy document must contain canonical JSON values")


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


__all__ = [
    "CompiledRemotePolicy",
    "REASON_CODES",
    "RemotePolicyDecision",
    "compile_policy",
    "evaluate_request",
]
