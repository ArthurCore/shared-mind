"""Deterministic, core-outside imports for pinned external memory formats.

Adapters only see immutable source bytes and public adapter values.  The
orchestrator is the sole holder of the transport-neutral ``WorkspaceService``;
no adapter receives a workspace, SQLite connection, or database path.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from ..canonical import canonical_json, sha256_bytes
from ..kernel import Kernel
from ..service import EXIT_INTERNAL_ERROR, OperationResult, WorkspaceService
from ..workspace import MAX_SOURCE_BYTES


CONTRACT_VERSION = "external-adapter-contract@1"
_PREDICATE_REGISTRY_VERSION = "1.0.0"
_PREDICATE_REGISTRY_HASH = (
    "sha256:61e27ee431c296eb5289bae28d6c4a6fe1426381b5e4eb4e07b1a88b62d43196"
)
_GUARD_DSL_VERSION = "guard-dsl@1"
_SOURCE_OPERATION = "REGISTER_SOURCE_REVISION"
_MAX_OPERATIONS = 128


class AdapterFailure(Exception):
    """A stable adapter-stage failure suitable for retry decisions."""

    def __init__(
        self,
        code: str,
        *,
        stage: str = "CREATE",
        retryable: bool = False,
        message: str | None = None,
    ) -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("adapter failure code must be a non-empty string")
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.message = message or code
        super().__init__(self.message)


@dataclass(frozen=True)
class AdapterSource:
    """One completely captured upstream source; partial streams are forbidden."""

    locator: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.locator, str) or not self.locator:
            raise ValueError("source locator must be a non-empty string")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("source media_type must be a non-empty string")
        if not isinstance(self.content, bytes):
            raise TypeError("source content must be immutable bytes")
        if len(self.content) > MAX_SOURCE_BYTES:
            raise AdapterFailure(
                "ADAPTER_SOURCE_TOO_LARGE",
                stage="CREATE",
                message=(
                    f"adapter source exceeds the {MAX_SOURCE_BYTES}-byte limit"
                ),
            )


@dataclass(frozen=True)
class AdapterProbe:
    adapter_name: str
    upstream_version: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class AdapterSnapshot:
    """Immutable content-addressed capture returned before any transformation."""

    adapter_name: str
    upstream_version: str
    source_locator: str
    media_type: str
    content: bytes
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("snapshot content must be immutable bytes")
        if not isinstance(self.content_hash, str):
            raise TypeError("snapshot content_hash must be a string")


@dataclass(frozen=True)
class AdapterSpec:
    name: str
    upstream_version: str
    stability: str
    allowed_inputs: tuple[str, ...]
    allowed_outputs: tuple[str, ...]
    source_only_default: bool
    semantic_promotion: str
    upstream_pin: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "upstream_version": self.upstream_version,
        }
        if self.upstream_pin is not None:
            result["upstream_pin"] = self.upstream_pin
        result.update(
            {
                "stability": self.stability,
                "allowed_inputs": list(self.allowed_inputs),
                "allowed_outputs": list(self.allowed_outputs),
                "source_only_default": self.source_only_default,
                "semantic_promotion": self.semantic_promotion,
            }
        )
        return result


@dataclass(frozen=True)
class AdapterCatalog:
    contract_version: str
    adapters: tuple[AdapterSpec, ...]


@dataclass(frozen=True)
class ReviewedMapping:
    """Human-reviewed permission to emit a narrow set of semantic operations."""

    mapping_id: str
    mapping_version: str
    reviewed_by: str
    reviewed_at: str
    allowed_operations: tuple[str, ...]

    def __post_init__(self) -> None:
        strings = (
            self.mapping_id,
            self.mapping_version,
            self.reviewed_by,
            self.reviewed_at,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("reviewed mapping fields must be non-empty strings")
        if (
            not isinstance(self.allowed_operations, tuple)
            or not self.allowed_operations
            or any(
                not isinstance(operation, str) or not operation
                for operation in self.allowed_operations
            )
        ):
            raise ValueError(
                "reviewed mapping allowed_operations must be a non-empty tuple"
            )


_CATALOG = AdapterCatalog(
    contract_version=CONTRACT_VERSION,
    adapters=(
        AdapterSpec(
            name="atomicstrata",
            upstream_version="62ef452b92ffd6480140671d5ccd199c6dc4b5aa",
            stability="restricted-citation-import",
            allowed_inputs=("JSON_EXPORT", "OKF_REFERENCE"),
            allowed_outputs=("SOURCE_REVISION",),
            source_only_default=True,
            semantic_promotion="REVIEWED_MAPPING_ONLY",
        ),
        AdapterSpec(
            name="qarinah",
            upstream_version="8541db37e0db0373af96fd228f90674272f59979",
            stability="stable-event-json",
            allowed_inputs=("EVENT_JSON",),
            allowed_outputs=("SOURCE_REVISION",),
            source_only_default=True,
            semantic_promotion="REVIEWED_MAPPING_ONLY",
        ),
        AdapterSpec(
            name="swarmvault",
            upstream_version="3.21.0",
            upstream_pin="815412d24298e59e5073ded1ddd6c0e6aee9b91b",
            stability="provisional-source-context-only",
            allowed_inputs=("SOURCE", "CONTEXT"),
            allowed_outputs=("SOURCE_REVISION",),
            source_only_default=True,
            semantic_promotion="REVIEWED_MAPPING_ONLY",
        ),
    ),
)
_SPECS = {spec.name: spec for spec in _CATALOG.adapters}


class ExternalAdapter(Protocol):
    spec: AdapterSpec

    def probe(self) -> AdapterProbe: ...

    def snapshot(self, probe: AdapterProbe) -> AdapterSnapshot: ...

    def validate(self, snapshot: AdapterSnapshot) -> None: ...

    def plan(
        self, snapshot: AdapterSnapshot, mapping: ReviewedMapping | None
    ) -> dict[str, Any]: ...


def adapter_catalog() -> AdapterCatalog:
    """Return the immutable, reviewed adapter catalog."""

    return _CATALOG


def create_adapter(name: str, source: AdapterSource) -> ExternalAdapter:
    """Create a pure bytes adapter for one exact reviewed upstream name."""

    if not isinstance(source, AdapterSource):
        raise TypeError("source must be an AdapterSource")
    spec = _SPECS.get(name)
    if spec is None:
        raise AdapterFailure("ADAPTER_NOT_SUPPORTED", stage="CREATE")
    return _SourceImportAdapter(spec, source)


class _SourceImportAdapter:
    def __init__(self, spec: AdapterSpec, source: AdapterSource) -> None:
        self.spec = spec
        self._source = source

    def probe(self) -> AdapterProbe:
        return AdapterProbe(
            adapter_name=self.spec.name,
            upstream_version=self.spec.upstream_version,
            capabilities=self.spec.allowed_inputs,
        )

    def snapshot(self, probe: AdapterProbe) -> AdapterSnapshot:
        if (
            probe.adapter_name != self.spec.name
            or probe.upstream_version != self.spec.upstream_version
        ):
            raise AdapterFailure("ADAPTER_PROBE_MISMATCH", stage="SNAPSHOT")
        return AdapterSnapshot(
            adapter_name=self.spec.name,
            upstream_version=self.spec.upstream_version,
            source_locator=self._source.locator,
            media_type=self._source.media_type,
            content=self._source.content,
            content_hash=sha256_bytes(self._source.content),
        )

    def validate(self, snapshot: AdapterSnapshot) -> None:
        document = _json_document(snapshot)
        if self.spec.name == "qarinah":
            _validate_qarinah(document)
        elif self.spec.name == "atomicstrata":
            _validate_atomicstrata(document)
        elif self.spec.name == "swarmvault":
            _validate_swarmvault(document)
        else:  # pragma: no cover - construction is catalog-closed
            raise AdapterFailure("ADAPTER_NOT_SUPPORTED", stage="VALIDATE")

    def plan(
        self, snapshot: AdapterSnapshot, mapping: ReviewedMapping | None
    ) -> dict[str, Any]:
        del mapping
        return _source_proposal(snapshot)


def run_import(
    adapter: ExternalAdapter,
    service: WorkspaceService,
    *,
    mapping: ReviewedMapping | None = None,
    max_attempts: int = 1,
) -> OperationResult:
    """Run one deterministic import, retrying only explicit retryable failures."""

    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
        raise TypeError("max_attempts must be an integer")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    if mapping is not None and not isinstance(mapping, ReviewedMapping):
        raise TypeError("mapping must be a ReviewedMapping or None")

    for attempt in range(1, max_attempts + 1):
        stage = "PROBE"
        try:
            probe = adapter.probe()
            stage = "SNAPSHOT"
            snapshot = adapter.snapshot(probe)
            _validate_snapshot_identity(adapter.spec, probe, snapshot)
            if len(snapshot.content) > MAX_SOURCE_BYTES:
                raise AdapterFailure(
                    "ADAPTER_SOURCE_TOO_LARGE",
                    stage="SNAPSHOT",
                    message=(
                        "adapter snapshot exceeds the "
                        f"{MAX_SOURCE_BYTES}-byte limit"
                    ),
                )
            if sha256_bytes(snapshot.content) != snapshot.content_hash:
                raise AdapterFailure(
                    "ADAPTER_SNAPSHOT_HASH_MISMATCH", stage="SNAPSHOT"
                )

            stage = "VALIDATE"
            adapter.validate(snapshot)

            stage = "PLAN"
            first_plan = adapter.plan(snapshot, mapping)
            second_plan = adapter.plan(snapshot, mapping)
            try:
                deterministic = canonical_json(first_plan) == canonical_json(
                    second_plan
                )
            except (TypeError, ValueError) as exc:
                raise AdapterFailure(
                    "ADAPTER_PLAN_INVALID",
                    stage="PLAN",
                    message=str(exc),
                ) from exc
            if not deterministic:
                raise AdapterFailure(
                    "ADAPTER_NONDETERMINISTIC_PLAN", stage="PLAN"
                )
            first_plan = _pin_plan_versions(
                first_plan, service.current_version_bundle()
            )
            _enforce_plan_policy(first_plan, mapping)

            validation = service.validate_proposal(first_plan)
            if not validation.ok:
                return _with_attempts(validation, attempt, nested=False)

            stage = "COMMIT"
            committed = service.commit_proposal(first_plan)
            return _with_attempts(committed, attempt, nested=True)
        except AdapterFailure as exc:
            if exc.retryable and attempt < max_attempts:
                continue
            return OperationResult(
                False,
                exc.code,
                data={"stage": exc.stage, "attempts": attempt},
                message=exc.message,
                exit_code=EXIT_INTERNAL_ERROR,
            )
        except Exception as exc:
            code = (
                "ADAPTER_COMMIT_FAILED"
                if stage == "COMMIT"
                else f"ADAPTER_{stage}_FAILED"
            )
            return OperationResult(
                False,
                code,
                data={"stage": stage, "attempts": attempt},
                message=str(exc),
                exit_code=EXIT_INTERNAL_ERROR,
            )

    raise RuntimeError("adapter retry loop ended without a result")


def _with_attempts(
    result: OperationResult, attempts: int, *, nested: bool
) -> OperationResult:
    data = dict(result.data) if isinstance(result.data, dict) else {}
    if nested:
        data["adapter"] = {"attempts": attempts}
    else:
        data["attempts"] = attempts
    return OperationResult(
        result.ok,
        result.code,
        data=data,
        errors=result.errors,
        message=result.message,
        exit_code=result.exit_code,
    )


def _validate_snapshot_identity(
    spec: AdapterSpec, probe: AdapterProbe, snapshot: AdapterSnapshot
) -> None:
    if not isinstance(probe, AdapterProbe):
        raise AdapterFailure("ADAPTER_PROBE_INVALID", stage="PROBE")
    if not isinstance(snapshot, AdapterSnapshot):
        raise AdapterFailure("ADAPTER_SNAPSHOT_INVALID", stage="SNAPSHOT")
    expected = (spec.name, spec.upstream_version)
    if (probe.adapter_name, probe.upstream_version) != expected:
        raise AdapterFailure("ADAPTER_PROBE_MISMATCH", stage="PROBE")
    if (snapshot.adapter_name, snapshot.upstream_version) != expected:
        raise AdapterFailure("ADAPTER_SNAPSHOT_IDENTITY_MISMATCH", stage="SNAPSHOT")


def _enforce_plan_policy(
    proposal: Any, mapping: ReviewedMapping | None
) -> None:
    if not isinstance(proposal, dict):
        return
    operations = proposal.get("operations")
    if not isinstance(operations, list):
        return
    if len(operations) > _MAX_OPERATIONS:
        raise AdapterFailure(
            "ADAPTER_OPERATION_LIMIT_EXCEEDED", stage="PLAN"
        )
    operation_names = tuple(
        operation.get("op") if isinstance(operation, dict) else None
        for operation in operations
    )
    if mapping is None:
        if any(name != _SOURCE_OPERATION for name in operation_names):
            raise AdapterFailure(
                "ADAPTER_REVIEWED_MAPPING_REQUIRED", stage="PLAN"
            )
        return
    allowed = set(mapping.allowed_operations)
    if any(name not in allowed for name in operation_names):
        raise AdapterFailure(
            "ADAPTER_MAPPING_OPERATION_NOT_ALLOWED", stage="PLAN"
        )


def _pin_plan_versions(proposal: Any, versions: dict[str, str]) -> Any:
    if not isinstance(proposal, dict):
        return proposal
    normalized = dict(proposal)
    normalized["versions"] = dict(versions)
    return normalized


def _json_document(snapshot: AdapterSnapshot) -> dict[str, Any]:
    if snapshot.media_type != "application/json":
        raise AdapterFailure("ADAPTER_MEDIA_TYPE_UNSUPPORTED", stage="VALIDATE")
    try:
        document = json.loads(snapshot.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterFailure(
            "ADAPTER_SOURCE_JSON_INVALID",
            stage="VALIDATE",
            message=str(exc),
        ) from exc
    if not isinstance(document, dict):
        raise AdapterFailure("ADAPTER_SOURCE_JSON_INVALID", stage="VALIDATE")
    return document


def _validate_qarinah(document: dict[str, Any]) -> None:
    required_strings = ("event_id", "event_type", "created_at")
    if any(
        not isinstance(document.get(field), str) or not document[field]
        for field in required_strings
    ) or not isinstance(document.get("payload"), dict):
        raise AdapterFailure("QARINAH_EVENT_INVALID", stage="VALIDATE")


def _validate_atomicstrata(document: dict[str, Any]) -> None:
    pages = document.get("pages")
    references = document.get("okf_references")
    if not isinstance(pages, list) or not pages:
        raise AdapterFailure("ATOMICSTRATA_EXPORT_INVALID", stage="VALIDATE")
    if not isinstance(references, list):
        raise AdapterFailure(
            "ATOMICSTRATA_OKF_REFERENCE_REQUIRED", stage="VALIDATE"
        )
    available = {
        item.get("reference")
        for item in references
        if isinstance(item, dict) and isinstance(item.get("reference"), str)
    }
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("citations"), list):
            raise AdapterFailure("ATOMICSTRATA_EXPORT_INVALID", stage="VALIDATE")
        for citation in page["citations"]:
            if (
                not isinstance(citation, dict)
                or citation.get("okf_reference") not in available
            ):
                raise AdapterFailure(
                    "ATOMICSTRATA_OKF_REFERENCE_REQUIRED", stage="VALIDATE"
                )


def _validate_swarmvault(document: dict[str, Any]) -> None:
    if document.get("swarmvault_version") != "3.21.0":
        raise AdapterFailure("SWARMVAULT_VERSION_MISMATCH", stage="VALIDATE")
    context = document.get("context")
    if not isinstance(context, dict) or not isinstance(context.get("sources"), list):
        raise AdapterFailure(
            "SWARMVAULT_SOURCE_CONTEXT_REQUIRED", stage="VALIDATE"
        )


def _source_proposal(snapshot: AdapterSnapshot) -> dict[str, Any]:
    identity = hashlib.sha256(
        snapshot.adapter_name.encode("utf-8")
        + b"\0"
        + snapshot.upstream_version.encode("utf-8")
        + b"\0"
        + snapshot.content
    ).hexdigest()
    source_revision = {
        "object_type": "SOURCE_REVISION",
        "source_id": f"adapter:{snapshot.adapter_name}",
        "revision_id": f"revision_{identity[:32]}",
        "content_hash": snapshot.content_hash,
        "blob_ref": (
            f"data:{snapshot.media_type};base64,"
            + base64.b64encode(snapshot.content).decode("ascii")
        ),
        "source_locator": snapshot.source_locator,
        "title": f"{snapshot.adapter_name} import",
        "media_type": snapshot.media_type,
        "captured_at": "2026-08-11T00:00:00Z",
        "registered_by": {
            "actor_id": f"service:adapter-{snapshot.adapter_name}",
            "actor_type": "SERVICE",
        },
    }
    return {
        "object_type": "PROPOSAL",
        "proposal_id": f"proposal_adapter_{identity[:32]}",
        "idempotency_key": f"adapter:{snapshot.adapter_name}:{identity[:48]}",
        "proposer": {
            "actor_id": f"service:adapter-{snapshot.adapter_name}",
            "actor_type": "SERVICE",
        },
        "proposed_at": "2026-08-11T00:00:00Z",
        "base_state_root": None,
        "versions": {
            "schema": Kernel.SUPPORTED_VERSIONS["schema"],
            "predicate_registry": _PREDICATE_REGISTRY_VERSION,
            "predicate_registry_hash": _PREDICATE_REGISTRY_HASH,
            "conflict_rules": Kernel.SUPPORTED_VERSIONS["conflict_rules"],
            "guard_dsl": _GUARD_DSL_VERSION,
            "projection": Kernel.SUPPORTED_VERSIONS["projection"],
        },
        "reads": [],
        "guards": [],
        "operations": [
            {
                "op_id": f"operation_register_{identity[:32]}",
                "op": _SOURCE_OPERATION,
                "source_revision": source_revision,
            }
        ],
    }


__all__ = [
    "AdapterCatalog",
    "AdapterFailure",
    "AdapterProbe",
    "AdapterSnapshot",
    "AdapterSource",
    "AdapterSpec",
    "ExternalAdapter",
    "ReviewedMapping",
    "adapter_catalog",
    "create_adapter",
    "run_import",
]
