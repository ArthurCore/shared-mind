from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from shared_mind.adapters import (
    AdapterFailure,
    AdapterProbe,
    AdapterSnapshot,
    AdapterSource,
    AdapterSpec,
)
from shared_mind.canonical import sha256_bytes, sha256_json
from shared_mind.kernel import Kernel


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "adapters"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_catalog() -> dict[str, Any]:
    return json.loads((FIXTURES / "catalog.v1.json").read_text(encoding="utf-8"))


def spec(name: str) -> AdapterSpec:
    record = next(
        item for item in fixture_catalog()["adapters"] if item["name"] == name
    )
    return AdapterSpec(
        name=record["name"],
        upstream_version=record["upstream_version"],
        stability=record["stability"],
        allowed_inputs=tuple(record["allowed_inputs"]),
        allowed_outputs=tuple(record["allowed_outputs"]),
        source_only_default=record["source_only_default"],
        semantic_promotion=record["semantic_promotion"],
        upstream_pin=record.get("upstream_pin"),
    )


def source_for(name: str) -> AdapterSource:
    filename = {
        "qarinah": "qarinah-event.v1.json",
        "atomicstrata": "atomicstrata-export.v1.json",
        "swarmvault": "swarmvault-context.v3.21.0.json",
    }[name]
    return AdapterSource(
        locator=f"fixture://{filename}",
        media_type="application/json",
        content=fixture_bytes(filename),
    )


class RecordingAdapter:
    """Pure fake exercising the public adapter lifecycle without network access."""

    def __init__(
        self,
        name: str = "qarinah",
        *,
        failure_stage: str | None = None,
        failure_code: str = "ADAPTER_FIXTURE_FAILURE",
        retryable: bool = False,
        fail_count: int = 1,
        semantic: bool = False,
        operation_count: int = 1,
        corrupt_snapshot_hash: bool = False,
        nondeterministic_plan: bool = False,
    ) -> None:
        self.spec = spec(name)
        self.source = source_for(name)
        self.failure_stage = failure_stage
        self.failure_code = failure_code
        self.retryable = retryable
        self.fail_count = fail_count
        self.semantic = semantic
        self.operation_count = operation_count
        self.corrupt_snapshot_hash = corrupt_snapshot_hash
        self.nondeterministic_plan = nondeterministic_plan
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._failures = 0
        self._plan_calls = 0

    def _record(self, stage: str, *arguments: Any) -> None:
        self.calls.append((stage, arguments))
        if self.failure_stage == stage and self._failures < self.fail_count:
            self._failures += 1
            raise AdapterFailure(
                self.failure_code,
                stage=stage,
                retryable=self.retryable,
            )

    def probe(self) -> AdapterProbe:
        self._record("PROBE")
        return AdapterProbe(
            adapter_name=self.spec.name,
            upstream_version=self.spec.upstream_version,
            capabilities=self.spec.allowed_inputs,
        )

    def snapshot(self, probe: AdapterProbe) -> AdapterSnapshot:
        self._record("SNAPSHOT", probe)
        content_hash = sha256_bytes(self.source.content)
        if self.corrupt_snapshot_hash:
            content_hash = "sha256:" + "0" * 64
        return AdapterSnapshot(
            adapter_name=self.spec.name,
            upstream_version=self.spec.upstream_version,
            source_locator=self.source.locator,
            media_type=self.source.media_type,
            content=self.source.content,
            content_hash=content_hash,
        )

    def validate(self, snapshot: AdapterSnapshot) -> None:
        self._record("VALIDATE", snapshot)

    def plan(self, snapshot: AdapterSnapshot, mapping: Any | None) -> dict[str, Any]:
        self._record("PLAN", snapshot, mapping)
        self._plan_calls += 1
        proposal = source_proposal(snapshot, operation_count=self.operation_count)
        if self.semantic:
            proposal["operations"] = [semantic_operation()]
        if self.nondeterministic_plan:
            proposal["proposal_id"] = (
                f"proposal_adapter_nondeterministic_{self._plan_calls:03d}"
            )
        return proposal


def source_proposal(
    snapshot: AdapterSnapshot, *, operation_count: int = 1
) -> dict[str, Any]:
    registry = json.loads(
        (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
            encoding="utf-8"
        )
    )
    digest = hashlib.sha256(
        snapshot.adapter_name.encode("utf-8")
        + b"\0"
        + snapshot.upstream_version.encode("utf-8")
        + b"\0"
        + snapshot.content
    ).hexdigest()
    revision = {
        "object_type": "SOURCE_REVISION",
        "source_id": f"adapter:{snapshot.adapter_name}",
        "revision_id": f"revision_{digest[:32]}",
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
    operation = {
        "op_id": f"operation_register_{digest[:32]}",
        "op": "REGISTER_SOURCE_REVISION",
        "source_revision": revision,
    }
    operations = [copy.deepcopy(operation) for _ in range(operation_count)]
    for index, item in enumerate(operations):
        item["op_id"] = f"operation_register_{index:03d}_{digest[:24]}"
        item["source_revision"]["revision_id"] = f"revision_{index:03d}_{digest[:28]}"
    return {
        "object_type": "PROPOSAL",
        "proposal_id": f"proposal_adapter_{digest[:32]}",
        "idempotency_key": f"adapter:{snapshot.adapter_name}:{digest[:48]}",
        "proposer": {
            "actor_id": f"service:adapter-{snapshot.adapter_name}",
            "actor_type": "SERVICE",
        },
        "proposed_at": "2026-08-11T00:00:00Z",
        "base_state_root": None,
        "versions": {
            "schema": Kernel.SUPPORTED_VERSIONS["schema"],
            "predicate_registry": registry["version"],
            "predicate_registry_hash": sha256_json(registry),
            "conflict_rules": Kernel.SUPPORTED_VERSIONS["conflict_rules"],
            "guard_dsl": registry["guard_dsl_version"],
            "projection": Kernel.SUPPORTED_VERSIONS["projection"],
        },
        "reads": [],
        "guards": [],
        "operations": operations,
    }


def semantic_operation() -> dict[str, Any]:
    fixture_set = json.loads(
        (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
            encoding="utf-8"
        )
    )
    objects = {
        item["name"]: item["object"] for item in fixture_set["typed_objects"]
    }
    return copy.deepcopy(objects["assert_postgresql_proposal"]["operations"][0])


def canonical_store_snapshot(workspace: Any) -> dict[str, Any]:
    from shared_mind.projection import project_json

    kernel = workspace.open_kernel()
    try:
        return {
            "ledger": kernel.connection.execute(
                "SELECT COUNT(*) FROM ledger"
            ).fetchone()[0],
            "receipts": kernel.connection.execute(
                "SELECT COUNT(*) FROM receipts"
            ).fetchone()[0],
            "sources": kernel.connection.execute(
                "SELECT COUNT(*) FROM sources"
            ).fetchone()[0],
            "state_root": kernel.state_root(),
            "projection": project_json(kernel),
            "verification": kernel.verify_ledger(),
        }
    finally:
        kernel.close()
