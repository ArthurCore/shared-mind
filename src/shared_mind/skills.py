"""Shared, versioned procedural-memory support.

Skills are project-scoped artifacts shared by every Agent/session.  They are
never copied into Agent-specific stores.  Candidate generation is separate
from TESTED/APPROVED promotion, and every version is content-addressed.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from .canonical import canonical_json, sha256_bytes, sha256_json
from .product_contract import validate_product_object
from .product_store import ProductStore, ProductStoreError, utc_now


SKILL_PACKAGE_VERSION = "skill-package@1"
_SAFE_SKILL_NAME = re.compile(r"[^a-z0-9._-]+")


class StepExecutor(Protocol):
    def __call__(self, step: str, context: Mapping[str, Any]) -> Any: ...


class ResourceLoader(Protocol):
    def __call__(self, resource: Mapping[str, Any]) -> bytes: ...


class SkillError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def build_skill_record(
    *,
    skill_id: str,
    version: int,
    purpose: str,
    triggers: Sequence[str],
    steps: Sequence[str],
    validation_rules: Sequence[Mapping[str, Any]],
    preconditions: Sequence[str] = (),
    resources: Sequence[Mapping[str, Any]] = (),
    expected_outputs: Sequence[str] = (),
    status: str = "DRAFT",
    provenance: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    timestamp = created_at or utc_now()
    core = {
        "skill_id": skill_id,
        "version": int(version),
        "purpose": purpose.strip(),
        "triggers": [item.strip() for item in triggers if item.strip()],
        "preconditions": [item.strip() for item in preconditions if item.strip()],
        "steps": [item.strip() for item in steps if item.strip()],
        "resources": [dict(item) for item in resources],
        "expected_outputs": [item.strip() for item in expected_outputs if item.strip()],
        "validation_rules": [dict(item) for item in validation_rules],
    }
    content_hash = sha256_json(core)
    record = {
        "object_type": "SKILL_RECORD",
        **core,
        "status": status,
        "content_hash": content_hash,
        "created_at": timestamp,
        "updated_at": timestamp,
        "document": core,
        "provenance": dict(provenance or {}),
    }
    issues = validate_product_object(record, "SkillRecord")
    if issues:
        raise SkillError("SKILL_INVALID", canonical_json(issues))
    return record


def create_skill(store: ProductStore, record: Mapping[str, Any]) -> dict[str, Any]:
    issues = validate_product_object(record, "SkillRecord")
    if issues:
        raise SkillError("SKILL_INVALID", canonical_json(issues))
    existing = store.get_skill(str(record["skill_id"]), version=int(record["version"]))
    if existing is not None:
        if existing["content_hash"] != record["content_hash"]:
            raise SkillError(
                "SKILL_VERSION_REUSE",
                f"Skill {record['skill_id']}@{record['version']} has different content.",
            )
        return existing
    _commit_skill_operation(
        store,
        {
            "op": "CREATE_SKILL",
            "skill": dict(record),
        },
        proposed_at=str(record["updated_at"]),
    )
    created = store.get_skill(str(record["skill_id"]), version=int(record["version"]))
    assert created is not None
    return created


def revise_skill(
    store: ProductStore,
    skill_id: str,
    *,
    expected_version: int,
    changes: Mapping[str, Any],
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = store.get_skill(skill_id)
    if current is None:
        raise SkillError("SKILL_NOT_FOUND", f"Skill not found: {skill_id}")
    if current["version"] != expected_version:
        raise SkillError(
            "SKILL_VERSION_MISMATCH",
            f"Skill {skill_id} is version {current['version']}, expected {expected_version}.",
        )
    document = dict(current["document"])
    for field in (
        "purpose",
        "triggers",
        "preconditions",
        "steps",
        "resources",
        "expected_outputs",
        "validation_rules",
    ):
        if field in changes:
            document[field] = changes[field]
    replacement = build_skill_record(
        skill_id=skill_id,
        version=expected_version + 1,
        purpose=str(document["purpose"]),
        triggers=list(document["triggers"]),
        preconditions=list(document.get("preconditions", [])),
        steps=list(document["steps"]),
        resources=list(document.get("resources", [])),
        expected_outputs=list(document.get("expected_outputs", [])),
        validation_rules=list(document["validation_rules"]),
        status="DRAFT",
        provenance=provenance or current["provenance"],
    )
    _commit_skill_operation(
        store,
        {
            "op": "REVISE_SKILL",
            "skill_id": skill_id,
            "expected_version": expected_version,
            "replacement_skill": replacement,
        },
        proposed_at=str(replacement["updated_at"]),
    )
    revised = store.get_skill(skill_id, version=expected_version + 1)
    assert revised is not None
    return revised


def mark_skill_tested(
    store: ProductStore,
    skill_id: str,
    version: int,
    *,
    test_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if test_evidence.get("passed") is not True:
        raise SkillError(
            "SKILL_TEST_EVIDENCE_FAILED",
            "A Skill can become TESTED only with explicit passing evidence.",
        )
    current = store.get_skill(skill_id, version=version)
    if current is None:
        raise SkillError("SKILL_NOT_FOUND", f"Skill not found: {skill_id}@{version}")
    if current["status"] in {"TESTED", "APPROVED"}:
        return current
    _commit_skill_operation(
        store,
        {
            "op": "MARK_SKILL_TESTED",
            "skill_id": skill_id,
            "version": version,
            "expected_status": "DRAFT",
            "test_evidence": dict(test_evidence),
        },
        proposed_at=utc_now(),
    )
    updated = store.get_skill(skill_id, version=version)
    assert updated is not None
    return updated


def approve_skill(
    store: ProductStore,
    skill_id: str,
    version: int,
    *,
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    current = store.get_skill(skill_id, version=version)
    if current is None:
        raise SkillError("SKILL_NOT_FOUND", f"Skill not found: {skill_id}@{version}")
    if current["status"] == "APPROVED":
        return current
    if current["status"] != "TESTED":
        raise SkillError(
            "SKILL_NOT_TESTED",
            f"Skill {skill_id}@{version} must be TESTED before approval.",
        )
    _commit_skill_operation(
        store,
        {
            "op": "APPROVE_SKILL",
            "skill_id": skill_id,
            "version": version,
            "expected_status": "TESTED",
            "approval": dict(approval),
        },
        proposed_at=utc_now(),
    )
    updated = store.get_skill(skill_id, version=version)
    assert updated is not None
    return updated


def deprecate_skill(
    store: ProductStore,
    skill_id: str,
    version: int,
    *,
    rationale: str,
) -> dict[str, Any]:
    current = store.get_skill(skill_id, version=version)
    if current is None:
        raise SkillError("SKILL_NOT_FOUND", f"Skill not found: {skill_id}@{version}")
    if current["status"] == "DEPRECATED":
        return current
    _commit_skill_operation(
        store,
        {
            "op": "DEPRECATE_SKILL",
            "skill_id": skill_id,
            "version": version,
            "expected_status": current["status"],
            "rationale": rationale,
        },
        proposed_at=utc_now(),
    )
    updated = store.get_skill(skill_id, version=version)
    assert updated is not None
    return updated


def _commit_skill_operation(
    store: ProductStore,
    operation: Mapping[str, Any],
    *,
    proposed_at: str,
) -> dict[str, Any]:
    operation_core = dict(operation)
    operation_digest = sha256_json(operation_core).split(":", 1)[1]
    operation_document = {
        "op_id": f"product_op_{operation_digest[:24]}",
        **operation_core,
    }
    proposal_digest = sha256_json(
        {"proposer": "service:shared-mind-product", "operation": operation_document}
    ).split(":", 1)[1]
    proposal = {
        "object_type": "PRODUCT_MUTATION_PROPOSAL",
        "proposal_id": f"product_proposal_{proposal_digest[:24]}",
        "idempotency_key": f"product-skill:{proposal_digest[:48]}",
        "proposer": "service:shared-mind-product",
        "proposed_at": proposed_at,
        "expected_product_state_hash": None,
        "operations": [operation_document],
    }
    receipt = store.commit_product_proposal(proposal)
    if receipt["outcome"] != "COMMITTED":
        code = receipt["reason_codes"][0] if receipt["reason_codes"] else "SKILL_MUTATION_FAILED"
        raise SkillError(code, f"Skill mutation returned {receipt['outcome']}.")
    return receipt


def select_skills(
    store: ProductStore,
    task: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    terms = {token.casefold() for token in re.findall(r"[\w.-]+", task) if len(token) > 1}
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for skill in store.list_skills(status="APPROVED"):
        document = skill["document"]
        haystack = " ".join(
            [
                str(document.get("purpose", "")),
                *[str(item) for item in document.get("triggers", [])],
                *[str(item) for item in document.get("preconditions", [])],
            ]
        ).casefold()
        score = sum(3 if term in " ".join(document.get("triggers", [])).casefold() else 1 for term in terms if term in haystack)
        if score:
            scored.append((score, str(skill["skill_id"]), skill))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [skill | {"selection_score": score} for score, _, skill in scored[:limit]]


def execute_skill(
    skill: Mapping[str, Any],
    *,
    executor: StepExecutor,
    context: Mapping[str, Any] | None = None,
    validators: Mapping[str, Callable[[Any, Mapping[str, Any]], bool]] | None = None,
) -> dict[str, Any]:
    if skill.get("status") not in {"TESTED", "APPROVED"}:
        raise SkillError("SKILL_NOT_EXECUTABLE", "Only TESTED or APPROVED skills can run.")
    document = skill["document"]
    execution_context = dict(context or {})
    outputs: list[Any] = []
    for index, step in enumerate(document["steps"]):
        value = executor(str(step), execution_context | {"step_index": index})
        outputs.append(value)
        execution_context["last_output"] = value
    validation_results: list[dict[str, Any]] = []
    for rule in document["validation_rules"]:
        rule_type = str(rule.get("type", "")).upper()
        target = outputs[-1] if outputs else None
        passed = False
        if rule_type == "CONTAINS":
            passed = str(rule.get("value", "")) in str(target)
        elif rule_type == "EQUALS":
            passed = target == rule.get("value")
        elif rule_type == "NON_EMPTY":
            passed = target not in (None, "", [], {})
        elif rule_type == "CALLBACK":
            name = str(rule.get("name", ""))
            callback = (validators or {}).get(name)
            passed = bool(callback(target, rule)) if callback else False
        validation_results.append({"rule": dict(rule), "passed": passed})
    return {
        "skill_id": skill["skill_id"],
        "version": skill["version"],
        "outputs": outputs,
        "validation_results": validation_results,
        "passed": bool(validation_results) and all(item["passed"] for item in validation_results),
    }


def export_skill_package(
    store: ProductStore,
    skill_id: str,
    destination: str | Path,
    *,
    version: int | None = None,
    resource_loader: ResourceLoader | None = None,
) -> dict[str, Any]:
    skill = store.get_skill(skill_id, version=version, approved_only=version is None)
    if skill is None:
        raise SkillError("SKILL_NOT_FOUND", f"Skill not found: {skill_id}")
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    resources_manifest: list[dict[str, Any]] = []
    resource_payloads: list[tuple[str, bytes]] = []
    for resource in skill["document"].get("resources", []):
        path = _safe_package_path(str(resource["path"]), prefix="resources")
        payload = resource_loader(resource) if resource_loader else b""
        actual_hash = sha256_bytes(payload)
        if payload and actual_hash != resource["content_hash"]:
            raise SkillError(
                "SKILL_RESOURCE_HASH_MISMATCH",
                f"Resource {resource['path']} does not match its content hash.",
            )
        resources_manifest.append(
            {
                "path": path,
                "content_hash": resource["content_hash"],
                "size": len(payload),
            }
        )
        resource_payloads.append((path, payload))
    skill_bytes = (canonical_json(skill) + "\n").encode("utf-8")
    manifest = {
        "package_version": SKILL_PACKAGE_VERSION,
        "skill_id": skill["skill_id"],
        "skill_version": skill["version"],
        "skill_hash": sha256_bytes(skill_bytes),
        "resources": resources_manifest,
    }
    manifest_bytes = (canonical_json(manifest) + "\n").encode("utf-8")
    with zipfile.ZipFile(destination_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _writestr_deterministic(archive, "manifest.json", manifest_bytes)
        _writestr_deterministic(archive, "skill.json", skill_bytes)
        for path, payload in sorted(resource_payloads):
            _writestr_deterministic(archive, path, payload)
    return {
        "path": str(destination_path),
        "package_hash": sha256_bytes(destination_path.read_bytes()),
        "manifest": manifest,
    }


def import_skill_package(store: ProductStore, package: str | Path) -> dict[str, Any]:
    package_path = Path(package)
    if not package_path.is_file():
        raise SkillError("SKILL_PACKAGE_NOT_FOUND", f"Package not found: {package_path}")
    if package_path.stat().st_size > 32 * 1024 * 1024:
        raise SkillError("SKILL_PACKAGE_TOO_LARGE", "Skill package exceeds 32 MiB.")
    with zipfile.ZipFile(package_path) as archive:
        names = archive.namelist()
        if len(names) > 512:
            raise SkillError("SKILL_PACKAGE_TOO_MANY_FILES", "Skill package has too many files.")
        for name in names:
            _safe_package_path(name)
        try:
            manifest_bytes = archive.read("manifest.json")
            skill_bytes = archive.read("skill.json")
        except KeyError as exc:
            raise SkillError("SKILL_PACKAGE_INVALID", f"Missing {exc.args[0]}.") from exc
        if len(manifest_bytes) > 1024 * 1024 or len(skill_bytes) > 4 * 1024 * 1024:
            raise SkillError("SKILL_PACKAGE_INVALID", "Skill package metadata is oversized.")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        skill = json.loads(skill_bytes.decode("utf-8"))
        if manifest.get("package_version") != SKILL_PACKAGE_VERSION:
            raise SkillError("SKILL_PACKAGE_VERSION_UNSUPPORTED", "Unsupported package version.")
        if manifest.get("skill_hash") != sha256_bytes(skill_bytes):
            raise SkillError("SKILL_PACKAGE_HASH_MISMATCH", "skill.json hash mismatch.")
        resource_by_path = {
            str(item["path"]): item for item in manifest.get("resources", [])
        }
        for path, item in resource_by_path.items():
            payload = archive.read(path)
            if len(payload) != item["size"] or sha256_bytes(payload) != item["content_hash"]:
                raise SkillError(
                    "SKILL_RESOURCE_HASH_MISMATCH", f"Resource verification failed: {path}"
                )
    return create_skill(store, skill)


def skill_id_from_purpose(purpose: str) -> str:
    slug = _SAFE_SKILL_NAME.sub("-", purpose.casefold()).strip("-._")[:96] or "skill"
    return f"skill:{slug}"


def _safe_package_path(path: str, *, prefix: str | None = None) -> str:
    pure = PurePosixPath(path.replace("\\", "/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SkillError("SKILL_PACKAGE_PATH_INVALID", f"Unsafe package path: {path}")
    normalized = pure.as_posix()
    if prefix and not normalized.startswith(prefix + "/"):
        normalized = f"{prefix}/{normalized}"
    return normalized


def _writestr_deterministic(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload)


__all__ = [
    "SKILL_PACKAGE_VERSION",
    "SkillError",
    "approve_skill",
    "build_skill_record",
    "create_skill",
    "deprecate_skill",
    "execute_skill",
    "export_skill_package",
    "import_skill_package",
    "mark_skill_tested",
    "revise_skill",
    "select_skills",
    "skill_id_from_purpose",
]
