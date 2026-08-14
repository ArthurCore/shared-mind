"""Product facade for Shared Mind's trusted, shared cognitive state."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .canonical import canonical_json, sha256_bytes, sha256_json
from .memory_views import ContextRouter, MemoryViewBuilder, MemoryViewError
from .product_contract import validate_product_object
from .product_ingest import (
    ExtractionLimits,
    IngestManager,
    ModelExtractor,
    ProductIngestError,
)
from .product_store import (
    PRODUCT_DATABASE_FILENAME,
    ProductStore,
    ProductStoreError,
    utc_now,
)
from .retrieval import RetrievalError, RetrievalService, VectorRanker
from .service import WorkspaceService
from .skills import (
    SkillError,
    StepExecutor,
    approve_skill,
    create_skill,
    execute_skill,
    export_skill_package,
    import_skill_package,
    mark_skill_tested,
    revise_skill,
)
from .workspace import Workspace, WorkspaceError


PRODUCT_API_VERSION = "shared-mind-product@1"
BACKUP_PACKAGE_VERSION = "shared-mind-backup@1"
BACKUP_MAX_FILES = 100_000
BACKUP_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
BACKUP_MAX_MEMBER_BYTES = 512 * 1024 * 1024
BACKUP_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


class ProductError(Exception):
    def __init__(self, code: str, message: str, *, data: Any | None = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


class ProductService:
    """High-level product operations over one Shared Mind workspace."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.store = ProductStore(workspace.control_root / PRODUCT_DATABASE_FILENAME)
        self.ingest_manager = IngestManager(workspace, self.store)
        self.views = MemoryViewBuilder(workspace, self.store)
        self.router = ContextRouter(workspace, self.store)
        self.retrieval = RetrievalService(workspace, self.store)
        self.kernel_service = WorkspaceService(workspace)

    @classmethod
    def open(cls, workspace: str | Path | Workspace) -> "ProductService":
        return cls(workspace if isinstance(workspace, Workspace) else Workspace.open(workspace))

    def close(self) -> None:
        self.store.close()

    def describe(self) -> dict[str, Any]:
        return {
            "api_version": PRODUCT_API_VERSION,
            "workspace": self.workspace.describe(),
            "product_database": str(self.store.path.relative_to(self.workspace.root)),
            "fts_enabled": self.store.fts_enabled,
            "audit": self.store.verify_audit(),
        }

    # ------------------------------------------------------------------
    # Ingest and draft review

    def ingest(
        self,
        paths: Sequence[str | Path],
        *,
        conversation_paths: Sequence[str | Path] = (),
        include_code: bool = True,
    ) -> dict[str, Any]:
        return self._translate(
            self.ingest_manager.ingest,
            paths,
            conversation_paths=conversation_paths,
            include_code=include_code,
        )

    def extract(
        self,
        batch_id: str,
        *,
        model_extractor: ModelExtractor | None = None,
        remote_policy_decision: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
        limits: ExtractionLimits | None = None,
    ) -> dict[str, Any]:
        return self._translate(
            self.ingest_manager.extract,
            batch_id,
            model_extractor=model_extractor,
            remote_policy_decision=remote_policy_decision,
            parameters=parameters,
            limits=limits,
        )

    def list_drafts(
        self,
        *,
        status: str | None = None,
        draft_kind: str | None = None,
        batch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_drafts(
            status=status, draft_kind=draft_kind, batch_id=batch_id
        )

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise ProductError("DRAFT_NOT_FOUND", f"Draft not found: {draft_id}")
        return draft

    def edit_draft(
        self,
        draft_id: str,
        document: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        if draft["status"] not in {"DRAFT", "REVIEWED"}:
            raise ProductError(
                "DRAFT_NOT_EDITABLE", f"Draft status is {draft['status']}."
            )
        self._validate_draft_document(draft["draft_kind"], document)
        with self.store.transaction():
            return self._translate(
                self.store.update_draft,
                draft_id,
                document=document,
                status="REVIEWED",
                expected_version=expected_version,
            )

    def reject_draft(self, draft_id: str, *, rationale: str) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        if draft["status"] in {"COMMITTED", "REJECTED", "EXPIRED"}:
            raise ProductError("DRAFT_FINAL", f"Draft is already {draft['status']}.")
        with self.store.transaction():
            updated = self.store.update_draft(draft_id, status="REJECTED")
            self.store.append_audit(
                "DRAFT_REJECTED",
                {"draft_id": draft_id, "rationale": rationale},
                object_id=draft_id,
            )
            return updated

    def expire_drafts(self, *, now: str | None = None) -> int:
        timestamp = now or utc_now()
        expired = 0
        for draft in self.store.list_drafts(status="DRAFT"):
            expires_at = draft.get("expires_at")
            if expires_at and expires_at <= timestamp:
                with self.store.transaction():
                    self.store.update_draft(draft["draft_id"], status="EXPIRED")
                expired += 1
        return expired

    def commit_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        if draft["status"] == "COMMITTED":
            return draft
        if draft["status"] not in {"DRAFT", "REVIEWED"}:
            raise ProductError(
                "DRAFT_NOT_COMMITTABLE", f"Draft status is {draft['status']}."
            )
        try:
            if draft["draft_kind"] == "KERNEL_PROPOSAL":
                validation = self.kernel_service.validate_proposal(draft["document"])
                if not validation.ok:
                    receipt = validation.as_dict()
                    final_status = "FAILED"
                else:
                    result = self.kernel_service.commit_proposal(draft["document"])
                    receipt = result.as_dict()
                    final_status = "COMMITTED" if result.ok else "FAILED"
            elif draft["draft_kind"] == "SKILL":
                skill = create_skill(self.store, draft["document"])
                receipt = {"ok": True, "code": "SKILL_STAGED", "data": skill}
                final_status = "COMMITTED"
            else:
                artifact = self.store.put_artifact(draft["document"])
                receipt = {"ok": True, "code": "ARTIFACT_STORED", "data": artifact}
                final_status = "COMMITTED"
        except (ProductStoreError, SkillError, WorkspaceError) as exc:
            receipt = {
                "ok": False,
                "code": getattr(exc, "code", "DRAFT_COMMIT_FAILED"),
                "message": str(exc),
            }
            final_status = "FAILED"
        with self.store.transaction():
            updated = self.store.update_draft(
                draft_id, status=final_status, receipt=receipt
            )
        if final_status != "COMMITTED":
            raise ProductError(
                "DRAFT_COMMIT_FAILED",
                f"Draft {draft_id} was not committed.",
                data=receipt,
            )
        return updated

    def commit_batch_drafts(
        self,
        batch_id: str,
        *,
        deterministic_only: bool = True,
    ) -> dict[str, Any]:
        committed: list[str] = []
        failed: list[dict[str, Any]] = []
        skipped: list[str] = []
        for draft in self.store.list_drafts(batch_id=batch_id):
            if draft["status"] == "COMMITTED":
                skipped.append(draft["draft_id"])
                continue
            if deterministic_only and draft["provenance"].get("mode") != "DETERMINISTIC":
                skipped.append(draft["draft_id"])
                continue
            try:
                self.commit_draft(draft["draft_id"])
            except ProductError as exc:
                failed.append(
                    {"draft_id": draft["draft_id"], "code": exc.code, "message": exc.message}
                )
            else:
                committed.append(draft["draft_id"])
        return {
            "batch_id": batch_id,
            "committed": committed,
            "failed": failed,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------
    # Views, context, retrieval

    def build_memory_views(self) -> dict[str, Any]:
        return self._translate(self.views.build_all)

    def build_indexes(self) -> dict[str, Any]:
        report = self._translate(self.retrieval.rebuild)
        return report.__dict__ if hasattr(report, "__dict__") else dict(report)

    def context(self, request: Mapping[str, Any]) -> dict[str, Any]:
        response = self._translate(self.router.route, request)
        self._record_telemetry(
            "CONTEXT_ROUTED",
            object_id=response["context_hash"],
            success=True,
            metadata={
                "request_hash": sha256_json(response["request"]),
                "context_hash": response["context_hash"],
                "included_bytes": response["budget"]["included_bytes"],
                "omitted": response["budget"]["omitted"],
            },
        )
        return response

    def search(
        self,
        query: str,
        *,
        kinds: Sequence[str] = (),
        limit: int = 20,
        vector_ranker: VectorRanker | None = None,
    ) -> dict[str, Any]:
        query_hash = sha256_json({"query": query, "kinds": list(kinds)})
        try:
            result = self._translate(
                self.retrieval.search,
                query,
                kinds=kinds,
                limit=limit,
                vector_ranker=vector_ranker,
            )
        except ProductError as exc:
            self._record_telemetry(
                "MEMORY_SEARCHED",
                object_id=query_hash,
                success=False,
                metadata={"query_hash": query_hash, "error_code": exc.code},
            )
            raise
        self._record_telemetry(
            "MEMORY_SEARCHED",
            object_id=query_hash,
            success=True,
            metadata={
                "query_hash": query_hash,
                "result_count": int(result.get("count", len(result.get("results", [])))),
                "result_ids": [
                    item.get("document_id") or item.get("id")
                    for item in result.get("results", [])[:20]
                ],
                "vector_enabled": vector_ranker is not None,
            },
        )
        return result

    def tool_call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        arguments_hash = sha256_json(dict(arguments))
        try:
            result = self._tool_call_impl(name, arguments)
        except ProductError as exc:
            self._record_telemetry(
                "PRODUCT_TOOL_CALLED",
                object_id=name,
                success=False,
                metadata={
                    "tool_name": name,
                    "arguments_hash": arguments_hash,
                    "error_code": exc.code,
                },
            )
            raise
        self._record_telemetry(
            "PRODUCT_TOOL_CALLED",
            object_id=name,
            success=True,
            metadata={"tool_name": name, "arguments_hash": arguments_hash},
        )
        return result

    def _tool_call_impl(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if name == "capabilities":
            return self.retrieval.capabilities()
        if name == "search":
            return self.search(
                str(arguments.get("query", "")),
                kinds=tuple(arguments.get("kinds", [])),
                limit=int(arguments.get("limit", 20)),
            )
        if name == "read_source_span":
            return self._translate(
                self.retrieval.read_source_span,
                str(arguments["revision_id"]),
                start_byte=int(arguments.get("start_byte", 0)),
                end_byte=arguments.get("end_byte"),
            )
        if name == "get_artifact":
            return self._translate(
                self.retrieval.get_artifact, str(arguments["artifact_id"])
            )
        if name == "get_skill":
            return self._translate(
                self.retrieval.get_skill,
                str(arguments["skill_id"]),
                version=arguments.get("version"),
            )
        if name == "find_symbol":
            return self.store.find_symbols(
                str(arguments["name"]), limit=int(arguments.get("limit", 20))
            )
        if name == "get_symbol":
            return self._translate(
                self.retrieval.get_symbol, str(arguments["symbol_id"])
            )
        if name == "impact_path":
            return self._translate(
                self.retrieval.impact_path,
                str(arguments["symbol_id"]),
                max_depth=int(arguments.get("max_depth", 4)),
                direction=str(arguments.get("direction", "BOTH")),
            )
        if name == "link_graph":
            return self.store.list_links(
                object_id=arguments.get("object_id"), relation=arguments.get("relation")
            )
        raise ProductError("TOOL_NOT_FOUND", f"Unknown product tool: {name}")

    # ------------------------------------------------------------------
    # Cold start and compounding loop

    def cold_start(
        self,
        paths: Sequence[str | Path],
        *,
        conversation_paths: Sequence[str | Path] = (),
        auto_commit_deterministic: bool = True,
        task: str = "Continue the highest-priority project work.",
        budget_bytes: int = 64 * 1024,
    ) -> dict[str, Any]:
        batch = self.ingest(paths, conversation_paths=conversation_paths)
        extraction = self.extract(batch["batch_id"])
        commit = (
            self.commit_batch_drafts(batch["batch_id"], deterministic_only=True)
            if auto_commit_deterministic
            else {"committed": [], "failed": [], "skipped": extraction["draft_ids"]}
        )
        views = self.build_memory_views()
        indexes = self.build_indexes()
        context = self.context(
            {
                "task": task,
                "purpose": self.workspace.purpose,
                "query": None,
                "references": [],
                "depth": "DETAIL",
                "budget_bytes": budget_bytes,
                "budget_tokens": None,
                "hints": {},
            }
        )
        atomic = self.views.atomic_records()
        source_map = [
            {
                "revision_id": record["object_id"],
                "source_id": record["document"]["source_id"],
                "title": record["title"],
                "media_type": record["document"]["media_type"],
                "source_locator": record["document"]["source_locator"],
            }
            for record in atomic
            if record["kind"] == "SOURCE_REVISION"
        ]
        source_map.sort(key=lambda item: (item["source_id"], item["revision_id"]))
        recommended_next_actions = self._recommended_next_actions(atomic)
        first_handoff = {
            **context,
            "source_map": source_map,
            "recommended_next_actions": recommended_next_actions,
        }
        first_handoff["handoff_hash"] = sha256_json(
            {
                key: value
                for key, value in first_handoff.items()
                if key != "handoff_hash"
            }
        )
        open_conflicts = len(self.workspace.list_conflicts("OPEN"))
        report = {
            "cold_start_version": "cold-start@1",
            "batch_id": batch["batch_id"],
            "ingest": batch["summary"],
            "extraction": {
                "created": extraction["created"],
                "duplicates": extraction["duplicates"],
                "failures": len(extraction["failures"]),
            },
            "review_queue": len(self.store.list_drafts(status="DRAFT")),
            "committed": len(commit["committed"]),
            "commit_failures": commit["failed"],
            "artifacts": views,
            "indexes": indexes,
            "unresolved_conflicts": open_conflicts,
            "first_handoff": first_handoff,
        }
        with self.store.transaction():
            self.store.append_audit(
                "COLD_START_COMPLETED", report, object_id=batch["batch_id"]
            )
        return report

    @staticmethod
    def _recommended_next_actions(
        records: Sequence[Mapping[str, Any]], *, limit: int = 12
    ) -> list[dict[str, Any]]:
        priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        actions: list[tuple[int, str, dict[str, Any]]] = []
        for record in records:
            if record["kind"] == "WORK_ITEM" and record["status"] in {
                "TODO",
                "DOING",
                "BLOCKED",
            }:
                priority = str(record["document"].get("priority", "P3"))
                actions.append(
                    (
                        priority_order.get(priority, 9),
                        str(record["object_id"]),
                        {
                            "kind": "WORK_ITEM",
                            "object_id": record["object_id"],
                            "priority": priority,
                            "action": record["title"],
                            "status": record["status"],
                            "blocker": record["document"].get("blocker"),
                        },
                    )
                )
            elif record["kind"] == "CONFLICT" and record["status"] == "OPEN":
                actions.append(
                    (
                        -2,
                        str(record["object_id"]),
                        {
                            "kind": "CONFLICT",
                            "object_id": record["object_id"],
                            "priority": "P0",
                            "action": f"Review unresolved conflict: {record['title']}",
                            "status": "OPEN",
                        },
                    )
                )
            elif record["kind"] == "OPEN_QUESTION" and record["status"] == "OPEN":
                actions.append(
                    (
                        4,
                        str(record["object_id"]),
                        {
                            "kind": "OPEN_QUESTION",
                            "object_id": record["object_id"],
                            "priority": "P2",
                            "action": f"Resolve open question: {record['title']}",
                            "status": "OPEN",
                        },
                    )
                )
        actions.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in actions[:limit]]

    def post_task_capture(
        self,
        task_id: str,
        trace: str | Mapping[str, Any] | Sequence[Any],
        *,
        auto_commit_deterministic: bool = False,
    ) -> dict[str, Any]:
        safe_id = "".join(character for character in task_id if character.isalnum() or character in "._-")
        if not safe_id:
            raise ProductError("TASK_ID_INVALID", "task_id has no safe characters")
        trace_root = self.workspace.source_root / "task-traces"
        trace_root.mkdir(parents=True, exist_ok=True)
        destination = trace_root / f"{safe_id}.jsonl"
        if isinstance(trace, str):
            content = trace
        elif isinstance(trace, Mapping):
            content = canonical_json(dict(trace))
        else:
            content = "\n".join(canonical_json(item) for item in trace)
        destination.write_text(content.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
        batch = self.ingest([], conversation_paths=[destination])
        extraction = self.extract(batch["batch_id"])
        commit = (
            self.commit_batch_drafts(batch["batch_id"])
            if auto_commit_deterministic
            else {"committed": [], "failed": [], "skipped": extraction["draft_ids"]}
        )
        consolidation = self.incremental_consolidation()
        return {
            "task_id": task_id,
            "batch": batch,
            "extraction": extraction,
            "commit": commit,
            "consolidation": consolidation,
        }

    def incremental_consolidation(self) -> dict[str, Any]:
        before = {
            item["artifact_id"]: item["dependency_digest"]
            for item in self.store.list_artifacts()
        }
        views = self.build_memory_views()
        indexes = self.build_indexes()
        after = {
            item["artifact_id"]: item["dependency_digest"]
            for item in self.store.list_artifacts()
        }
        changed = sorted(
            artifact_id
            for artifact_id in set(before) | set(after)
            if before.get(artifact_id) != after.get(artifact_id)
        )
        return {"changed_artifact_ids": changed, "views": views, "indexes": indexes}

    # ------------------------------------------------------------------
    # Skill lifecycle

    def revise_skill(
        self,
        skill_id: str,
        *,
        expected_version: int,
        changes: Mapping[str, Any],
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._translate(
            revise_skill,
            self.store,
            skill_id,
            expected_version=expected_version,
            changes=changes,
            provenance=provenance,
        )

    def test_skill(
        self,
        skill_id: str,
        version: int,
        *,
        executor: StepExecutor,
        context: Mapping[str, Any] | None = None,
        validators: Mapping[str, Callable[[Any, Mapping[str, Any]], bool]] | None = None,
    ) -> dict[str, Any]:
        skill = self.store.get_skill(skill_id, version=version)
        if skill is None:
            raise ProductError("SKILL_NOT_FOUND", f"Skill not found: {skill_id}@{version}")
        candidate = skill | {"status": "TESTED"}
        execution = self._translate(
            execute_skill,
            candidate,
            executor=executor,
            context=context,
            validators=validators,
        )
        if not execution["passed"]:
            self._record_telemetry(
                "SKILL_EXECUTED",
                object_id=f"{skill_id}@{version}",
                success=False,
                metadata={
                    "skill_id": skill_id,
                    "version": version,
                    "validation_count": len(execution.get("validation", [])),
                },
            )
            raise ProductError("SKILL_VALIDATION_FAILED", "Skill validation did not pass.", data=execution)
        updated = self._translate(
            mark_skill_tested,
            self.store,
            skill_id,
            version,
            test_evidence=execution,
        )
        self._record_telemetry(
            "SKILL_EXECUTED",
            object_id=f"{skill_id}@{version}",
            success=True,
            metadata={
                "skill_id": skill_id,
                "version": version,
                "validation_count": len(execution.get("validation", [])),
            },
        )
        return updated

    def record_skill_test(
        self, skill_id: str, version: int, *, evidence: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._translate(
            mark_skill_tested,
            self.store,
            skill_id,
            version,
            test_evidence=evidence,
        )

    def approve_skill(
        self, skill_id: str, version: int, *, approval: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._translate(
            approve_skill, self.store, skill_id, version, approval=approval
        )

    def export_skill(
        self,
        skill_id: str,
        destination: str | Path,
        *,
        version: int | None = None,
        resource_loader: Callable[[Mapping[str, Any]], bytes] | None = None,
    ) -> dict[str, Any]:
        return self._translate(
            export_skill_package,
            self.store,
            skill_id,
            destination,
            version=version,
            resource_loader=resource_loader,
        )

    def import_skill(self, package: str | Path) -> dict[str, Any]:
        return self._translate(import_skill_package, self.store, package)

    # ------------------------------------------------------------------
    # Governance, audit, backup

    def catalog(self) -> dict[str, Any]:
        atomic = self.views.atomic_records()
        artifacts = self.store.list_artifacts()
        skills = self.store.list_skills()
        drafts = self.store.list_drafts()
        items = []
        for record in atomic:
            items.append(
                {
                    "id": record["object_id"],
                    "kind": record["kind"],
                    "status": record["status"],
                    "title": record["title"],
                    "provenance": {
                        "source_revision_ids": record["source_revision_ids"],
                        "projection_ref": record["projection_ref"],
                    },
                }
            )
        for artifact in artifacts:
            items.append(
                {
                    "id": artifact["artifact_id"],
                    "kind": artifact["artifact_type"],
                    "status": artifact["status"],
                    "title": artifact["title"],
                    "version": artifact["version"],
                    "provenance": artifact["provenance"],
                }
            )
        for skill in skills:
            items.append(
                {
                    "id": f"{skill['skill_id']}@{skill['version']}",
                    "kind": "SKILL",
                    "status": skill["status"],
                    "title": skill["document"]["purpose"],
                    "version": skill["version"],
                    "provenance": skill["provenance"],
                }
            )
        for draft in drafts:
            items.append(
                {
                    "id": draft["draft_id"],
                    "kind": f"DRAFT/{draft['draft_kind']}",
                    "status": draft["status"],
                    "title": draft["draft_id"],
                    "version": draft["version"],
                    "provenance": draft["provenance"],
                }
            )
        items.sort(key=lambda item: (item["kind"], item["id"]))
        return {"items": items, "count": len(items)}

    def review_queue(self) -> dict[str, Any]:
        return {
            "drafts": [
                draft
                for draft in self.store.list_drafts()
                if draft["status"] in {"DRAFT", "REVIEWED", "FAILED"}
            ],
            "stale_artifacts": self.store.list_artifacts(status="STALE"),
            "open_conflicts": self.workspace.list_conflicts("OPEN"),
            "skills_to_test": self.store.list_skills(status="DRAFT"),
            "skills_to_approve": self.store.list_skills(status="TESTED"),
        }

    def verify(self) -> dict[str, Any]:
        kernel = self.workspace.open_kernel()
        try:
            kernel_report = kernel.verify_ledger()
            state_root = kernel.state_root()
        finally:
            kernel.close()
        audit = self.store.verify_audit()
        skill_replay = self.store.verify_skill_replay()
        artifact_issues: list[str] = []
        for artifact in self.store.list_artifacts(status="READY"):
            provenance = artifact["provenance"]
            if not provenance.get("builder_version") or not provenance.get("kernel_state_root"):
                artifact_issues.append(artifact["artifact_id"])
        derived_views = self._verify_derived_views()
        return {
            "valid": (
                bool(kernel_report.get("valid"))
                and audit["valid"]
                and skill_replay["valid"]
                and derived_views["valid"]
                and not artifact_issues
            ),
            "kernel": kernel_report,
            "kernel_state_root": state_root,
            "product_audit": audit,
            "skill_replay": skill_replay,
            "derived_views": derived_views,
            "product_state_hash": self.store.product_state_hash(),
            "artifact_provenance_issues": artifact_issues,
        }

    def _verify_derived_views(self) -> dict[str, Any]:
        managed_types = {"ATOMIC_MAP", "SCENARIO", "CORE_CONTEXT"}
        managed_rows = [
            artifact
            for artifact in self.store.list_artifacts()
            if artifact["artifact_type"] in managed_types
        ]
        if not managed_rows:
            return {
                "valid": True,
                "checked": False,
                "expected_count": 0,
                "actual_count": 0,
                "missing": [],
                "unexpected": [],
                "mismatched": [],
                "expected_hash": sha256_json([]),
                "actual_hash": sha256_json([]),
            }
        with tempfile.TemporaryDirectory(prefix="shared-mind-derived-verify-") as temporary:
            verification_store = ProductStore(Path(temporary) / PRODUCT_DATABASE_FILENAME)
            try:
                builder = MemoryViewBuilder(self.workspace, verification_store)
                builder.build_all()
                expected_rows = [
                    artifact
                    for artifact in verification_store.list_artifacts(status="READY")
                    if artifact["artifact_type"] in managed_types
                ]
            finally:
                verification_store.close()
        actual_rows = [
            artifact
            for artifact in managed_rows
            if artifact["status"] == "READY"
        ]
        expected = {
            artifact["artifact_id"]: _artifact_verification_document(artifact)
            for artifact in expected_rows
        }
        actual = {
            artifact["artifact_id"]: _artifact_verification_document(artifact)
            for artifact in actual_rows
        }
        expected_ids = set(expected)
        actual_ids = set(actual)
        missing = sorted(expected_ids - actual_ids)
        unexpected = sorted(actual_ids - expected_ids)
        mismatched = sorted(
            artifact_id
            for artifact_id in expected_ids & actual_ids
            if expected[artifact_id] != actual[artifact_id]
        )
        expected_documents = [expected[key] for key in sorted(expected)]
        actual_documents = [actual[key] for key in sorted(actual)]
        return {
            "valid": not missing and not unexpected and not mismatched,
            "checked": True,
            "expected_count": len(expected),
            "actual_count": len(actual),
            "missing": missing,
            "unexpected": unexpected,
            "mismatched": mismatched,
            "expected_hash": sha256_json(expected_documents),
            "actual_hash": sha256_json(actual_documents),
        }

    def export_backup(self, destination: str | Path) -> dict[str, Any]:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        verify = self.verify()
        if not verify["valid"]:
            raise ProductError("BACKUP_SOURCE_INVALID", "Workspace verification failed.", data=verify)
        with tempfile.TemporaryDirectory(prefix="shared-mind-backup-") as temporary:
            root = Path(temporary) / "snapshot"
            root.mkdir()
            control = root / ".shared-mind"
            control.mkdir()
            _sqlite_backup(self.workspace.database_path, control / self.workspace.database_path.name)
            self.store.checkpoint()
            _sqlite_backup(self.store.path, control / self.store.path.name)
            shutil.copy2(self.workspace.config_path, control / self.workspace.config_path.name)
            shutil.copy2(self.workspace.registry_path, control / self.workspace.registry_path.name)
            for source_root in (self.workspace.blob_root, self.workspace.source_root, self.workspace.projection_root):
                if source_root.exists():
                    relative = source_root.relative_to(self.workspace.root)
                    shutil.copytree(source_root, root / relative, dirs_exist_ok=True, symlinks=False)
            files = []
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ProductError("BACKUP_SYMLINK_DENIED", str(path))
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    payload = path.read_bytes()
                    files.append(
                        {"path": relative, "size": len(payload), "content_hash": sha256_bytes(payload)}
                    )
            manifest = {
                "package_version": BACKUP_PACKAGE_VERSION,
                "created_at": utc_now(),
                "kernel_state_root": verify["kernel_state_root"],
                "product_state_hash": verify["product_state_hash"],
                "files": files,
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            with zipfile.ZipFile(destination_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        _zip_write(archive, path.relative_to(root).as_posix(), path.read_bytes())
        return {
            "path": str(destination_path),
            "package_hash": sha256_bytes(destination_path.read_bytes()),
            "manifest": manifest,
        }

    @classmethod
    def restore_backup(
        cls, package: str | Path, destination: str | Path
    ) -> dict[str, Any]:
        package_path = Path(package)
        destination_path = Path(destination).expanduser().resolve()
        if destination_path.exists() and any(destination_path.iterdir()):
            raise ProductError("RESTORE_DESTINATION_NOT_EMPTY", str(destination_path))
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        staging_parent = Path(
            tempfile.mkdtemp(
                prefix=".shared-mind-restore-", dir=str(destination_path.parent)
            )
        )
        staging_root = staging_parent / "workspace"
        staging_root.mkdir()
        try:
            with zipfile.ZipFile(package_path) as archive:
                infos = archive.infolist()
                if len(infos) > BACKUP_MAX_FILES + 1:
                    raise ProductError(
                        "BACKUP_TOO_MANY_FILES", f"Archive contains {len(infos)} entries."
                    )
                names = [info.filename for info in infos]
                if len(names) != len(set(names)):
                    raise ProductError(
                        "BACKUP_DUPLICATE_ENTRY", "Archive contains duplicate member names."
                    )
                for info in infos:
                    _safe_archive_path(info.filename)
                    if info.is_dir():
                        raise ProductError("BACKUP_DIRECTORY_ENTRY_DENIED", info.filename)
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ProductError("BACKUP_SYMLINK_DENIED", info.filename)
                    if info.file_size > BACKUP_MAX_MEMBER_BYTES:
                        raise ProductError("BACKUP_MEMBER_TOO_LARGE", info.filename)
                if "manifest.json" not in names:
                    raise ProductError("BACKUP_MANIFEST_MISSING", "manifest.json is missing")
                manifest_info = archive.getinfo("manifest.json")
                if manifest_info.file_size > BACKUP_MAX_MANIFEST_BYTES:
                    raise ProductError(
                        "BACKUP_MANIFEST_TOO_LARGE", str(manifest_info.file_size)
                    )
                try:
                    manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProductError("BACKUP_MANIFEST_INVALID", str(exc)) from exc
                if not isinstance(manifest, Mapping) or not isinstance(
                    manifest.get("files"), list
                ):
                    raise ProductError(
                        "BACKUP_MANIFEST_INVALID", "Manifest files must be a list."
                    )
                if manifest.get("package_version") != BACKUP_PACKAGE_VERSION:
                    raise ProductError(
                        "BACKUP_VERSION_UNSUPPORTED",
                        str(manifest.get("package_version")),
                    )
                expected: dict[str, Mapping[str, Any]] = {}
                declared_total = 0
                for raw_item in manifest["files"]:
                    if not isinstance(raw_item, Mapping):
                        raise ProductError(
                            "BACKUP_MANIFEST_INVALID", "Manifest file entry is not an object."
                        )
                    name = _safe_archive_path(str(raw_item.get("path", "")))
                    if name == "manifest.json" or name in expected:
                        raise ProductError("BACKUP_DUPLICATE_ENTRY", name)
                    size = raw_item.get("size")
                    content_hash = raw_item.get("content_hash")
                    if (
                        not isinstance(size, int)
                        or size < 0
                        or size > BACKUP_MAX_MEMBER_BYTES
                        or not isinstance(content_hash, str)
                    ):
                        raise ProductError("BACKUP_MANIFEST_INVALID", name)
                    declared_total += size
                    if declared_total > BACKUP_MAX_TOTAL_BYTES:
                        raise ProductError(
                            "BACKUP_TOTAL_TOO_LARGE", str(declared_total)
                        )
                    expected[name] = raw_item
                allowed_names = {"manifest.json", *expected}
                unexpected = sorted(set(names) - allowed_names)
                missing = sorted(set(expected) - set(names))
                if unexpected:
                    raise ProductError("BACKUP_UNEXPECTED_ENTRY", unexpected[0])
                if missing:
                    raise ProductError("BACKUP_FILE_MISSING", missing[0])
                actual_total = sum(
                    archive.getinfo(name).file_size for name in expected
                )
                if actual_total > BACKUP_MAX_TOTAL_BYTES:
                    raise ProductError("BACKUP_TOTAL_TOO_LARGE", str(actual_total))
                for name, item in expected.items():
                    info = archive.getinfo(name)
                    if info.file_size != item["size"]:
                        raise ProductError("BACKUP_FILE_HASH_MISMATCH", name)
                    payload = archive.read(info)
                    if sha256_bytes(payload) != item["content_hash"]:
                        raise ProductError("BACKUP_FILE_HASH_MISMATCH", name)
                    target = staging_root / PurePosixPath(name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)

            service = cls.open(staging_root)
            try:
                verify = service.verify()
                if not verify["valid"]:
                    raise ProductError(
                        "RESTORE_VERIFICATION_FAILED",
                        "Restored workspace verification failed.",
                        data=verify,
                    )
                if verify["kernel_state_root"] != manifest["kernel_state_root"]:
                    raise ProductError(
                        "RESTORE_KERNEL_ROOT_MISMATCH", "Kernel state root mismatch"
                    )
                if verify["product_state_hash"] != manifest["product_state_hash"]:
                    raise ProductError(
                        "RESTORE_PRODUCT_HASH_MISMATCH", "Product state hash mismatch"
                    )
            finally:
                service.close()

            if destination_path.exists():
                destination_path.rmdir()
            os.replace(staging_root, destination_path)
            return {
                "workspace": str(destination_path),
                "verify": verify,
                "manifest": manifest,
            }
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)

    # ------------------------------------------------------------------
    # Product evaluation

    def memory_quality_metrics(self) -> dict[str, Any]:
        kernel = self.workspace.open_kernel()
        try:
            source_content = {
                row["revision_id"]: bytes(row["content"])
                for row in kernel.connection.execute("SELECT revision_id, content FROM sources")
            }
            evidence_rows = list(kernel.connection.execute("SELECT document FROM evidence"))
            evidence_valid = 0
            for row in evidence_rows:
                evidence = json.loads(row["document"])
                selector = evidence["selector"]
                content = source_content.get(evidence["source_revision_id"], b"")
                excerpt = content[selector["start_byte"] : selector["end_byte"]]
                if (
                    excerpt.decode("utf-8", errors="replace") == selector["excerpt"]
                    and sha256_bytes(excerpt) == selector["excerpt_hash"]
                ):
                    evidence_valid += 1
            open_conflicts = self.workspace.list_conflicts("OPEN")
            claim_ids = {
                row["claim_id"]
                for row in kernel.connection.execute("SELECT claim_id FROM claims")
            }
            conflict_complete = sum(
                1
                for conflict in open_conflicts
                if set(conflict["members"]).issubset(claim_ids)
            )
        finally:
            kernel.close()
        artifacts = self.store.list_artifacts()
        provenance_objects = [
            *self.store.list_drafts(),
            *artifacts,
            *self.store.list_skills(),
        ]
        provenance_complete = sum(
            1 for item in provenance_objects if bool(item.get("provenance"))
        )
        return {
            "evidence_validity": evidence_valid / len(evidence_rows) if evidence_rows else 1.0,
            "open_conflict_member_recall": conflict_complete / len(open_conflicts) if open_conflicts else 1.0,
            "stale_artifact_rate": sum(item["status"] == "STALE" for item in artifacts) / len(artifacts) if artifacts else 0.0,
            "provenance_completeness": provenance_complete / len(provenance_objects) if provenance_objects else 1.0,
            "draft_duplicate_rows": self.store.connection.execute(
                """
                SELECT COUNT(*) FROM (
                  SELECT batch_id, dependency_digest, draft_kind, COUNT(*) AS n
                  FROM drafts GROUP BY batch_id, dependency_digest, draft_kind HAVING n > 1
                )
                """
            ).fetchone()[0],
        }

    def context_routing_metrics(
        self,
        request: Mapping[str, Any],
        *,
        expected_ids: Sequence[str] = (),
        repetitions: int = 3,
    ) -> dict[str, Any]:
        responses = [self.router.route(request) for _ in range(repetitions)]
        hashes = [item["context_hash"] for item in responses]
        core_hashes = [sha256_json(item["core_context"]) for item in responses]
        included = {
            trace["id"]
            for trace in responses[0]["selection_trace"]
            if trace["included"]
        }
        expected = set(expected_ids)
        irrelevant = included - expected if expected else set()
        return {
            "context_hashes": hashes,
            "cross_client_parity": len(set(hashes)) == 1,
            "core_context_parity": len(set(core_hashes)) == 1,
            "core_context_hashes": core_hashes,
            "core_context_preserved": all(bool(item.get("core_context")) for item in responses),
            "relevant_recall": len(included.intersection(expected)) / len(expected) if expected else 1.0,
            "included_ids": sorted(included),
            "irrelevant_ids": sorted(irrelevant),
            "irrelevant_context_rate": (
                len(irrelevant) / len(included) if included and expected else 0.0
            ),
            "context_bytes": responses[0]["budget"]["included_bytes"],
            "omitted": responses[0]["budget"]["omitted"],
        }

    def skill_reuse_benchmark(
        self,
        skill_id: str,
        version: int,
        *,
        executor: StepExecutor,
        baseline: Callable[[], Mapping[str, Any]],
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        baseline_result = dict(baseline())
        skill = self.store.get_skill(skill_id, version=version)
        if skill is None:
            raise ProductError("SKILL_NOT_FOUND", f"Skill not found: {skill_id}@{version}")
        skill_result = execute_skill(skill | {"status": "TESTED"}, executor=executor, context=context)
        result = {
            "skill_id": skill_id,
            "version": version,
            "baseline": baseline_result,
            "with_skill": skill_result,
            "success_improved": bool(skill_result["passed"]) and not bool(baseline_result.get("passed")),
            "turn_reduction": max(0, int(baseline_result.get("turns", 0)) - len(skill_result["outputs"])),
        }
        self._record_telemetry(
            "SKILL_REUSE_EVALUATED",
            object_id=f"{skill_id}@{version}",
            success=bool(skill_result["passed"]),
            metadata={
                "skill_id": skill_id,
                "version": version,
                "success_improved": result["success_improved"],
                "turn_reduction": result["turn_reduction"],
            },
        )
        return result

    def cold_start_benchmark(
        self,
        handoff: Mapping[str, Any],
        *,
        manual_explanation: str,
        expected_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        handoff_bytes = len(canonical_json(handoff).encode("utf-8"))
        baseline_bytes = len(manual_explanation.encode("utf-8"))
        included = {
            trace["id"]
            for trace in handoff.get("selection_trace", [])
            if trace.get("included")
        }
        expected = set(expected_ids)
        return {
            "baseline_bytes": baseline_bytes,
            "handoff_bytes": handoff_bytes,
            "byte_reduction": 1.0 - handoff_bytes / baseline_bytes if baseline_bytes else 0.0,
            "expected_recall": len(included.intersection(expected)) / len(expected) if expected else 1.0,
            "context_hash": handoff.get("context_hash"),
        }

    # ------------------------------------------------------------------

    def _validate_draft_document(self, kind: str, document: Mapping[str, Any]) -> None:
        if kind == "KERNEL_PROPOSAL":
            result = self.kernel_service.validate_proposal(document)
            if not result.ok:
                raise ProductError("DRAFT_DOCUMENT_INVALID", "Kernel Proposal is invalid.", data=result.as_dict())
        elif kind == "SKILL":
            issues = validate_product_object(document, "SkillRecord")
            if issues:
                raise ProductError("DRAFT_DOCUMENT_INVALID", canonical_json(issues))
        elif kind == "MEMORY_ARTIFACT":
            issues = validate_product_object(document, "DerivedMemoryArtifact")
            if issues:
                raise ProductError("DRAFT_DOCUMENT_INVALID", canonical_json(issues))
        else:
            raise ProductError("DRAFT_KIND_UNSUPPORTED", kind)

    @staticmethod
    def _telemetry_event(
        event_type: str,
        *,
        object_id: str | None,
        success: bool | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        occurred_at = utc_now()
        nonce = time.time_ns()
        digest = sha256_json(
            {
                "event_type": event_type,
                "object_id": object_id,
                "success": success,
                "metadata": dict(metadata),
                "occurred_at": occurred_at,
                "nonce": nonce,
            }
        ).split(":", 1)[1]
        return {
            "object_type": "PRODUCT_TELEMETRY_EVENT",
            "event_id": f"event_{digest[:24]}",
            "event_type": event_type,
            "object_id": object_id,
            "occurred_at": occurred_at,
            "success": success,
            "metadata": dict(metadata),
        }

    def _record_telemetry(
        self,
        event_type: str,
        *,
        object_id: str | None,
        success: bool | None,
        metadata: Mapping[str, Any],
    ) -> None:
        event = self._telemetry_event(
            event_type,
            object_id=object_id,
            success=success,
            metadata=metadata,
        )
        with self.store.transaction():
            self.store.record_telemetry(event)

    @staticmethod
    def _translate(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except ProductError:
            raise
        except (
            ProductIngestError,
            ProductStoreError,
            MemoryViewError,
            RetrievalError,
            SkillError,
            WorkspaceError,
        ) as exc:
            raise ProductError(
                getattr(exc, "code", "PRODUCT_OPERATION_FAILED"),
                getattr(exc, "message", str(exc)),
                data=getattr(exc, "data", None),
            ) from exc


def _artifact_verification_document(artifact: Mapping[str, Any]) -> dict[str, Any]:
    provenance = artifact.get("provenance", {})
    return {
        "artifact_id": artifact["artifact_id"],
        "artifact_type": artifact["artifact_type"],
        "scope": artifact["scope"],
        "title": artifact["title"],
        "status": artifact["status"],
        "dependency_digest": artifact["dependency_digest"],
        "builder_version": artifact["builder_version"],
        "document": artifact["document"],
        "provenance": {
            "kernel_state_root": provenance.get("kernel_state_root"),
            "builder_version": provenance.get("builder_version"),
            "member_object_ids": provenance.get("member_object_ids", []),
            "member_dependency_digests": provenance.get(
                "member_dependency_digests", {}
            ),
        },
    }


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _safe_archive_path(name: str) -> str:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ProductError("BACKUP_PATH_INVALID", name)
    return pure.as_posix()


def _zip_write(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    _safe_archive_path(name)
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload)


__all__ = [
    "BACKUP_PACKAGE_VERSION",
    "PRODUCT_API_VERSION",
    "ProductError",
    "ProductService",
]
