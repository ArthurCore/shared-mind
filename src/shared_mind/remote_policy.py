"""Pure, deterministic authorization policy for future remote adapters.

This module evaluates already-authenticated identity bindings.  It performs no
authentication, transport, file, environment, or network work; adapters must
provide the authenticated binding out of band from the untrusted request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote


REASON_CODES = (
    "ACTOR_BINDING_MISMATCH",
    "DISCLOSURE_FIELD_DENIED",
    "ENDPOINT_PIN_MISMATCH",
    "MISSING_TRUST_BINDING",
    "OPERATION_SCOPE_DENIED",
    "REGISTRY_VERSION_MISMATCH",
    "REMOTE_VERSION_PIN_MISMATCH",
    "SENSITIVITY_DENIED",
    "SOURCE_SCOPE_DENIED",
    "TRUST_BINDING_NOT_ALLOWED",
    "UNKNOWN_CAPABILITY",
)

_TRUST_FIELDS = ("binding_id", "issuer", "subject", "actor_id")


@dataclass(frozen=True)
class CompiledRemotePolicy:
    """An immutable handle to one canonicalized remote policy document."""

    policy_hash: str
    _document: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class RemotePolicyDecision:
    """Canonical audit decision with a defensive JSON-compatible projection."""

    _document: Mapping[str, Any] = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        """Return an independent copy of the canonical audit document."""

        return _json_copy(self._document)


def compile_policy(document: Mapping[str, Any]) -> CompiledRemotePolicy:
    """Canonicalize and compile a deny-by-default remote policy."""

    if not isinstance(document, Mapping):
        raise TypeError("remote policy document must be a mapping")
    canonical_document = _json_copy(document)
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

    document = policy._document
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
    normalized = unquote(source_ref)
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
    if not isinstance(document.get("registry_version"), str):
        raise ValueError("remote policy registry_version is required")
    endpoint_pin = _required_mapping(document, "endpoint_pin")
    for field_name in (
        "endpoint_id",
        "origin",
        "protocol_version",
        "adapter_version",
    ):
        if not isinstance(endpoint_pin.get(field_name), str):
            raise ValueError(f"remote policy endpoint_pin.{field_name} is required")
    if not _mapping_values(document.get("trust_bindings")):
        raise ValueError("remote policy requires at least one trust binding")
    if not _mapping_values(document.get("source_labels")):
        raise ValueError("remote policy requires at least one source label")
    if not _mapping_values(document.get("capability_scopes")):
        raise ValueError("remote policy requires at least one capability scope")


def _required_mapping(
    document: Mapping[str, Any], field_name: str
) -> Mapping[str, Any]:
    value = document.get(field_name)
    if not isinstance(value, Mapping):
        raise ValueError(f"remote policy {field_name} must be an object")
    return value


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
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_hash(value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


__all__ = [
    "CompiledRemotePolicy",
    "REASON_CODES",
    "RemotePolicyDecision",
    "compile_policy",
    "evaluate_request",
]
