"""Trusted bulk ingest and reviewable extraction for Shared Mind."""

from __future__ import annotations

import base64
import concurrent.futures
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .canonical import canonical_json, sha256_bytes, sha256_json
from .product_contract import validate_product_object
from .product_store import ProductStore, utc_now
from .service import WorkspaceService
from .skills import build_skill_record, skill_id_from_purpose
from .workspace import Workspace, WorkspaceError


INGEST_MANIFEST_VERSION = "ingest-manifest@1"
DETERMINISTIC_EXTRACTOR_VERSION = "deterministic-directives@1"
MAX_INGEST_FILES = 10_000
MAX_INGEST_FILE_BYTES = 1024 * 1024
MAX_INGEST_TOTAL_BYTES = 256 * 1024 * 1024
MAX_EXTRACTION_OPERATIONS = 128
MAX_EXTRACTION_CHARS = 2 * 1024 * 1024
MAX_MODEL_RESULT_BYTES = 4 * 1024 * 1024
MAX_EXTRACTION_TOKENS = 512 * 1024
DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 60.0
_SUPPORTED_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".jsonl",
    ".json",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".toml",
    ".yaml",
    ".yml",
}
_SKIP_DIRECTORIES = {
    ".git",
    ".shared-mind",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
}
_SEMANTIC_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_DIRECTIVE = re.compile(r"^\s*(FACT|DECISION|QUESTION|WORK|SKILL)\s*:\s*(.+?)\s*$", re.I)


class ProductIngestError(Exception):
    def __init__(self, code: str, message: str, *, data: Any | None = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


@dataclass(frozen=True)
class ExtractionLimits:
    max_operations: int = MAX_EXTRACTION_OPERATIONS
    max_characters: int = MAX_EXTRACTION_CHARS
    max_result_bytes: int = MAX_MODEL_RESULT_BYTES
    max_tokens: int = MAX_EXTRACTION_TOKENS
    timeout_seconds: float = DEFAULT_EXTRACTION_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for field_name in (
            "max_operations",
            "max_characters",
            "max_result_bytes",
            "max_tokens",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")


class ModelExtractor(Protocol):
    extractor_id: str
    extractor_version: str
    model: str
    prompt_version: str

    def extract(
        self,
        *,
        source_revision: Mapping[str, Any],
        content: str,
        limits: ExtractionLimits,
    ) -> Mapping[str, Any]: ...


class IngestManager:
    """Bulk source registration and candidate extraction."""

    def __init__(self, workspace: Workspace, store: ProductStore):
        self.workspace = workspace
        self.store = store
        self.kernel_service = WorkspaceService(workspace)

    def ingest(
        self,
        paths: Sequence[str | Path],
        *,
        conversation_paths: Sequence[str | Path] = (),
        include_code: bool = True,
        max_files: int = MAX_INGEST_FILES,
        max_file_bytes: int = MAX_INGEST_FILE_BYTES,
        max_total_bytes: int = MAX_INGEST_TOTAL_BYTES,
    ) -> dict[str, Any]:
        roots = [(Path(path), False) for path in paths]
        roots.extend((Path(path), True) for path in conversation_paths)
        candidates = self._collect_candidates(
            roots,
            include_code=include_code,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        manifest_entries = [
            {
                "source_path": item["source_path"],
                "source_id": item["source_id"],
                "fingerprint": item["fingerprint"],
                "media_type": item["media_type"],
            }
            for item in candidates
        ]
        manifest_hash = sha256_json(
            {"manifest_version": INGEST_MANIFEST_VERSION, "items": manifest_entries}
        )
        batch_id = f"batch_{manifest_hash.split(':', 1)[1][:24]}"
        existing = self.store.get_batch(batch_id)
        if existing and existing.get("status") == "COMPLETED":
            return existing | {"reused": True, "resumed": False}
        prior_items = {
            item["item_id"]: item for item in self.store.list_ingest_items(batch_id)
        } if existing else {}
        now = utc_now()
        batch = {
            "object_type": "INGEST_BATCH",
            "batch_id": batch_id,
            "manifest_hash": manifest_hash,
            "status": "RUNNING",
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
            "items": [],
            "options": {
                "include_code": include_code,
                "max_files": max_files,
                "max_file_bytes": max_file_bytes,
                "max_total_bytes": max_total_bytes,
                "resume_count": int((existing or {}).get("options", {}).get("resume_count", 0)) + (1 if existing else 0),
            },
            "summary": {
                "total": len(candidates),
                "imported": 0,
                "unchanged": 0,
                "failed": 0,
                "skipped": 0,
            },
        }
        issues = validate_product_object(batch, "IngestBatch")
        if issues:
            raise ProductIngestError("INGEST_BATCH_INVALID", canonical_json(issues))
        with self.store.transaction():
            self.store.put_batch(batch)

        results: list[dict[str, Any]] = []
        for candidate in candidates:
            item_digest = sha256_json({
                "source_id": candidate["source_id"],
                "fingerprint": candidate["fingerprint"],
            }).split(":", 1)[1]
            item_id = f"item_{item_digest[:24]}"
            previous = prior_items.get(item_id)
            item = {
                "batch_id": batch_id,
                "item_id": item_id,
                "source_path": candidate["source_path"],
                "source_id": candidate["source_id"],
                "fingerprint": candidate["fingerprint"],
                "media_type": candidate["media_type"],
                "captured_at": candidate["captured_at"],
                "status": "PENDING",
                "revision_id": None,
                "error_code": None,
            }
            if (
                previous
                and previous.get("status") in {"IMPORTED", "UNCHANGED"}
                and previous.get("revision_id")
            ):
                item["status"] = previous["status"]
                item["revision_id"] = previous["revision_id"]
            elif self.store.was_imported(item["source_id"], item["fingerprint"]):
                item["status"] = "UNCHANGED"
                item["revision_id"] = self._registered_revision_id(
                    item["source_id"], candidate["content"]
                )
            else:
                try:
                    source, outcome = self._register_source(
                        content=candidate["content"],
                        source_id=item["source_id"],
                        title=candidate["title"],
                        media_type=item["media_type"],
                        captured_at=item["captured_at"],
                        locator=candidate["source_locator"],
                    )
                    item["revision_id"] = source["revision_id"]
                    item["status"] = "IMPORTED" if outcome == "COMMITTED" else "UNCHANGED"
                except (WorkspaceError, ProductIngestError) as exc:
                    item["status"] = "FAILED"
                    item["error_code"] = getattr(exc, "code", "INGEST_FAILED")
            with self.store.transaction():
                self.store.put_ingest_item(item)
            results.append(item)

        summary = {
            "total": len(results),
            "imported": sum(item["status"] == "IMPORTED" for item in results),
            "unchanged": sum(item["status"] == "UNCHANGED" for item in results),
            "failed": sum(item["status"] == "FAILED" for item in results),
            "skipped": sum(item["status"] == "SKIPPED" for item in results),
        }
        batch["items"] = results
        batch["summary"] = summary
        batch["status"] = (
            "FAILED"
            if results and summary["failed"] == len(results)
            else "PARTIAL"
            if summary["failed"]
            else "COMPLETED"
        )
        batch["updated_at"] = utc_now()
        with self.store.transaction():
            self.store.put_batch(batch)
        return batch | {"reused": False, "resumed": bool(existing)}

    def extract(
        self,
        batch_id: str,
        *,
        model_extractor: ModelExtractor | None = None,
        remote_policy_decision: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
        limits: ExtractionLimits | None = None,
    ) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise ProductIngestError("INGEST_BATCH_NOT_FOUND", f"Batch not found: {batch_id}")
        effective_limits = limits or ExtractionLimits()
        if model_extractor is not None:
            self._require_remote_allow(remote_policy_decision)
        drafts_created: list[str] = []
        duplicates = 0
        failures: list[dict[str, str]] = []
        items = self.store.list_ingest_items(batch_id)
        for item in items:
            if item["status"] not in {"IMPORTED", "UNCHANGED"} or not item.get("revision_id"):
                continue
            try:
                source, content = self._load_source(str(item["revision_id"]))
                if len(content) > effective_limits.max_characters:
                    raise ProductIngestError(
                        "EXTRACTION_SOURCE_TOO_LARGE",
                        f"Source exceeds {effective_limits.max_characters} characters.",
                    )
                estimated_tokens = (len(content.encode("utf-8")) + 3) // 4
                if estimated_tokens > effective_limits.max_tokens:
                    raise ProductIngestError(
                        "EXTRACTION_TOKEN_LIMIT",
                        f"Estimated input tokens {estimated_tokens} exceed {effective_limits.max_tokens}.",
                    )
                deadline = time.monotonic() + effective_limits.timeout_seconds
                if model_extractor is None:
                    result = self._deterministic_extract(
                        source, content, effective_limits, deadline=deadline
                    )
                    provenance = self._provenance(
                        source,
                        extractor_id="shared-mind-deterministic",
                        extractor_version=DETERMINISTIC_EXTRACTOR_VERSION,
                        mode="DETERMINISTIC",
                        model=None,
                        prompt_version="directive-parser@1",
                        parameters=parameters or {},
                        disclosure_policy=None,
                    )
                else:
                    result = self._run_model_extractor(
                        model_extractor,
                        source=source,
                        content=content,
                        limits=effective_limits,
                    )
                    encoded = canonical_json(result).encode("utf-8")
                    if len(encoded) > effective_limits.max_result_bytes:
                        raise ProductIngestError(
                            "EXTRACTION_RESULT_TOO_LARGE",
                            "Model extractor result exceeds the configured byte cap.",
                        )
                    provenance = self._provenance(
                        source,
                        extractor_id=model_extractor.extractor_id,
                        extractor_version=model_extractor.extractor_version,
                        mode="MODEL_BACKED",
                        model=model_extractor.model,
                        prompt_version=model_extractor.prompt_version,
                        parameters=parameters or {},
                        disclosure_policy=dict(remote_policy_decision or {}),
                    )
                created, repeated = self._stage_result(
                    batch_id=batch_id,
                    source=source,
                    result=result,
                    provenance=provenance,
                )
                drafts_created.extend(created)
                duplicates += repeated
            except Exception as exc:
                failures.append(
                    {
                        "revision_id": str(item.get("revision_id")),
                        "code": getattr(exc, "code", "EXTRACTION_FAILED"),
                        "message": str(exc),
                    }
                )
        return {
            "batch_id": batch_id,
            "draft_ids": sorted(drafts_created),
            "created": len(drafts_created),
            "duplicates": duplicates,
            "failures": failures,
            "status": "PARTIAL" if failures and drafts_created else "FAILED" if failures else "COMPLETED",
        }

    # ------------------------------------------------------------------
    # Source collection and registration

    def _collect_candidates(
        self,
        roots: Sequence[tuple[Path, bool]],
        *,
        include_code: bool,
        max_files: int,
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> list[dict[str, Any]]:
        discovered: list[tuple[Path, Path, bool]] = []
        projection_root = self.workspace.projection_root.resolve()
        for raw_root, conversation in roots:
            expanded = raw_root.expanduser()
            if expanded.is_symlink():
                raise ProductIngestError(
                    "INGEST_SYMLINK_DENIED", f"Symlink root denied: {expanded}"
                )
            root = expanded.resolve()
            if not root.exists():
                raise ProductIngestError("INGEST_PATH_NOT_FOUND", f"Path not found: {root}")
            if root.is_file():
                if not self._is_within(root, projection_root):
                    discovered.append((root, root.parent, conversation))
            else:
                for path in sorted(root.rglob("*")):
                    if any(part in _SKIP_DIRECTORIES for part in path.relative_to(root).parts):
                        continue
                    if path.is_symlink() or not path.is_file():
                        continue
                    # Projections are disposable views over canonical state.
                    # Exclude only this workspace's configured projection
                    # root; a user directory that merely happens to be named
                    # "projections" remains valid source material.
                    if self._is_within(path.resolve(), projection_root):
                        continue
                    discovered.append((path, root, conversation))
        if len(discovered) > max_files:
            raise ProductIngestError(
                "INGEST_TOO_MANY_FILES", f"Found {len(discovered)} files, limit is {max_files}."
            )
        total = 0
        candidates: list[dict[str, Any]] = []
        for path, root, explicit_conversation in discovered:
            suffix = path.suffix.casefold()
            is_code = suffix in {".py", ".js", ".jsx", ".ts", ".tsx"}
            if suffix not in _SUPPORTED_SUFFIXES or (is_code and not include_code):
                continue
            size = path.stat().st_size
            if size > max_file_bytes:
                raise ProductIngestError(
                    "INGEST_FILE_TOO_LARGE", f"{path} is {size} bytes, limit is {max_file_bytes}."
                )
            total += size
            if total > max_total_bytes:
                raise ProductIngestError(
                    "INGEST_TOTAL_TOO_LARGE",
                    f"Input is {total} bytes, limit is {max_total_bytes}.",
                )
            content = path.read_bytes()
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProductIngestError("INGEST_NOT_UTF8", f"{path}: {exc}") from exc
            relative = path.relative_to(root).as_posix()
            conversation = explicit_conversation or suffix == ".jsonl"
            kind = "conversation" if conversation else "code" if is_code else "document"
            source_id = self._source_id(kind, relative)
            fingerprint = sha256_bytes(content)
            captured_at = self._captured_at(text, conversation=conversation)
            candidates.append(
                {
                    "source_path": str(path),
                    "source_locator": path.as_uri(),
                    "title": path.name,
                    "source_id": source_id,
                    "fingerprint": fingerprint,
                    "media_type": self._media_type(path, conversation=conversation),
                    "captured_at": captured_at,
                    "content": content,
                }
            )
        candidates.sort(key=lambda item: (item["source_id"], item["fingerprint"]))
        return candidates

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _source_id(kind: str, relative: str) -> str:
        slug = _SEMANTIC_UNSAFE.sub("-", relative.casefold()).strip("-._")
        digest = sha256(relative.encode("utf-8")).hexdigest()[:12]
        prefix = slug[:100].rstrip("-._") or "source"
        return f"{kind}:{prefix}-{digest}"

    @staticmethod
    def _media_type(path: Path, *, conversation: bool) -> str:
        if conversation:
            return "application/x-ndjson"
        return {
            ".md": "text/markdown",
            ".markdown": "text/markdown",
            ".txt": "text/plain",
            ".json": "application/json",
            ".jsonl": "application/x-ndjson",
            ".py": "text/x-python",
            ".js": "text/javascript",
            ".jsx": "text/javascript",
            ".ts": "text/typescript",
            ".tsx": "text/typescript",
            ".toml": "application/toml",
            ".yaml": "application/yaml",
            ".yml": "application/yaml",
        }.get(path.suffix.casefold(), "text/plain")

    @staticmethod
    def _captured_at(text: str, *, conversation: bool) -> str:
        if conversation:
            timestamps: list[str] = []
            for line in text.splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, Mapping):
                    continue
                for field in ("timestamp", "created_at", "time", "date"):
                    normalized = _normalize_timestamp(value.get(field))
                    if normalized:
                        timestamps.append(normalized)
                        break
            if timestamps:
                return min(timestamps)
        return utc_now()

    @staticmethod
    def _registered_revision_id(source_id: str, content: bytes) -> str:
        identity_hash = sha256(source_id.encode("utf-8") + b"\0" + content).hexdigest()
        return f"revision_{identity_hash[:32]}"

    def _register_source(
        self,
        *,
        content: bytes,
        source_id: str,
        title: str,
        media_type: str,
        captured_at: str,
        locator: str,
    ) -> tuple[dict[str, Any], str]:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProductIngestError("INGEST_NOT_UTF8", str(exc)) from exc
        content_hash = sha256_bytes(content)
        canonical_media_type = (
            media_type
            if media_type in {"text/markdown", "text/plain", "application/json"}
            else "application/json"
            if "json" in media_type
            else "text/plain"
        )
        identity_hash = sha256(source_id.encode("utf-8") + b"\0" + content).hexdigest()
        revision_id = self._registered_revision_id(source_id, content)
        kernel = self.workspace.open_kernel()
        try:
            row = kernel.connection.execute(
                "SELECT document FROM sources WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if row:
                return json.loads(row["document"]), "UNCHANGED"
            source_revision = {
                "object_type": "SOURCE_REVISION",
                "source_id": source_id,
                "revision_id": revision_id,
                "content_hash": content_hash,
                "blob_ref": f"data:{canonical_media_type};base64,{base64.b64encode(content).decode('ascii')}",
                "source_locator": locator,
                "title": title,
                "media_type": canonical_media_type,
                "captured_at": captured_at,
                "registered_by": {
                    "actor_id": "service:shared-mind-product",
                    "actor_type": "SERVICE",
                },
            }
            proposal = {
                "object_type": "PROPOSAL",
                "proposal_id": f"proposal_register_{identity_hash[:32]}",
                "idempotency_key": f"source-register:{identity_hash[:48]}",
                "proposer": source_revision["registered_by"],
                "proposed_at": captured_at,
                "base_state_root": None,
                "versions": self.kernel_service.current_version_bundle(),
                "reads": [],
                "guards": [],
                "operations": [
                    {
                        "op_id": f"operation_register_{identity_hash[:32]}",
                        "op": "REGISTER_SOURCE_REVISION",
                        "source_revision": source_revision,
                    }
                ],
            }
            receipt = kernel.commit(proposal)
            if receipt.outcome not in {"COMMITTED", "FACT_CONFLICT"}:
                raise ProductIngestError(
                    "SOURCE_REGISTRATION_FAILED",
                    f"Source registration returned {receipt.outcome}: {receipt.reason_codes}",
                )
            blob_name = content_hash.split(":", 1)[1]
            self.workspace._preserve_blob(  # type: ignore[attr-defined]
                self.workspace.blob_root / blob_name, content, content_hash
            )
            return source_revision, "COMMITTED"
        finally:
            kernel.close()

    def _load_source(self, revision_id: str) -> tuple[dict[str, Any], str]:
        kernel = self.workspace.open_kernel()
        try:
            row = kernel.connection.execute(
                "SELECT document, content FROM sources WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if row is None:
                raise ProductIngestError("SOURCE_REVISION_NOT_FOUND", revision_id)
            return json.loads(row["document"]), bytes(row["content"]).decode("utf-8")
        finally:
            kernel.close()

    # ------------------------------------------------------------------
    # Extraction and staging

    def _deterministic_extract(
        self,
        source: Mapping[str, Any],
        content: str,
        limits: ExtractionLimits,
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        registry = json.loads(self.workspace.registry_path.read_text(encoding="utf-8"))
        predicate_by_key = {item["key"]: item for item in registry["predicates"]}
        operations: list[dict[str, Any]] = []
        skills: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        conversation = (
            source.get("media_type") == "application/x-ndjson"
            or str(source.get("source_id", "")).startswith("conversation:")
        )
        for directive in _iter_directives(content, conversation):
            if deadline is not None and time.monotonic() > deadline:
                raise ProductIngestError(
                    "EXTRACTION_TIMEOUT",
                    f"Extraction exceeded {limits.timeout_seconds} seconds.",
                )
            if len(operations) >= limits.max_operations:
                diagnostics.append({"code": "OPERATION_LIMIT_REACHED"})
                break
            try:
                kind = directive["kind"]
                if kind == "FACT":
                    operations.append(
                        _fact_operation(source, directive, predicate_by_key)
                    )
                elif kind == "DECISION":
                    operations.append(_decision_operation(source, directive))
                elif kind == "QUESTION":
                    operations.append(_question_operation(source, directive))
                elif kind == "WORK":
                    operations.append(_work_operation(source, directive))
                else:
                    skills.append(_skill_candidate(source, directive))
            except ProductIngestError as exc:
                diagnostics.append(
                    {
                        "code": exc.code,
                        "message": exc.message,
                        "start_byte": directive["start_byte"],
                    }
                )
        return {"operations": operations, "skills": skills, "diagnostics": diagnostics}

    @staticmethod
    def _run_model_extractor(
        extractor: ModelExtractor,
        *,
        source: Mapping[str, Any],
        content: str,
        limits: ExtractionLimits,
    ) -> dict[str, Any]:
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="shared-mind-extractor"
        )
        future = executor.submit(
            extractor.extract,
            source_revision=source,
            content=content,
            limits=limits,
        )
        try:
            result = future.result(timeout=limits.timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise ProductIngestError(
                "EXTRACTION_TIMEOUT",
                f"Model extraction exceeded {limits.timeout_seconds} seconds.",
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if not isinstance(result, Mapping):
            raise ProductIngestError(
                "EXTRACTION_RESULT_INVALID", "Model extractor must return an object."
            )
        return dict(result)

    def _stage_result(
        self,
        *,
        batch_id: str,
        source: Mapping[str, Any],
        result: Mapping[str, Any],
        provenance: Mapping[str, Any],
    ) -> tuple[list[str], int]:
        operations = list(result.get("operations", []))
        skills = list(result.get("skills", []))
        if len(operations) > MAX_EXTRACTION_OPERATIONS:
            raise ProductIngestError(
                "EXTRACTION_OPERATION_LIMIT", "Extraction returned too many operations."
            )
        created: list[str] = []
        duplicates = 0
        if operations:
            proposal_core = {
                "source_revision_id": source["revision_id"],
                "operations": operations,
                "extractor": {
                    key: value
                    for key, value in provenance.items()
                    if key != "generated_at"
                },
            }
            digest = sha256_json(proposal_core)
            suffix = digest.split(":", 1)[1]
            proposal = {
                "object_type": "PROPOSAL",
                "proposal_id": f"proposal_extract_{suffix[:32]}",
                "idempotency_key": f"extract:{suffix[:56]}",
                "proposer": {
                    "actor_id": "service:shared-mind-extractor",
                    "actor_type": "SERVICE",
                },
                "proposed_at": provenance["generated_at"],
                "base_state_root": None,
                "versions": self.kernel_service.current_version_bundle(),
                "reads": [],
                "guards": [],
                "operations": operations,
            }
            draft = self._draft(
                batch_id=batch_id,
                draft_kind="KERNEL_PROPOSAL",
                dependency_digest=digest,
                document=proposal,
                provenance=provenance,
            )
            with self.store.transaction():
                if self.store.put_draft(draft):
                    created.append(draft["draft_id"])
                else:
                    duplicates += 1
        for skill in skills:
            digest = sha256_json(
                {
                    "source_revision_id": source["revision_id"],
                    "skill": skill,
                    "extractor": {
                        key: value
                        for key, value in provenance.items()
                        if key != "generated_at"
                    },
                }
            )
            draft = self._draft(
                batch_id=batch_id,
                draft_kind="SKILL",
                dependency_digest=digest,
                document=skill,
                provenance=provenance,
            )
            with self.store.transaction():
                if self.store.put_draft(draft):
                    created.append(draft["draft_id"])
                else:
                    duplicates += 1
        return created, duplicates

    @staticmethod
    def _draft(
        *,
        batch_id: str,
        draft_kind: str,
        dependency_digest: str,
        document: Mapping[str, Any],
        provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        draft = {
            "object_type": "DRAFT_PROPOSAL",
            "draft_id": f"draft_{dependency_digest.split(':', 1)[1][:24]}",
            "batch_id": batch_id,
            "draft_kind": draft_kind,
            "status": "DRAFT",
            "version": 1,
            "dependency_digest": dependency_digest,
            "created_at": now,
            "updated_at": now,
            "expires_at": None,
            "document": dict(document),
            "provenance": dict(provenance),
        }
        issues = validate_product_object(draft, "DraftProposal")
        if issues:
            raise ProductIngestError("DRAFT_INVALID", canonical_json(issues))
        return draft

    @staticmethod
    def _provenance(
        source: Mapping[str, Any],
        *,
        extractor_id: str,
        extractor_version: str,
        mode: str,
        model: str | None,
        prompt_version: str,
        parameters: Mapping[str, Any],
        disclosure_policy: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "extractor_id": extractor_id,
            "extractor_version": extractor_version,
            "mode": mode,
            "model": model,
            "prompt_version": prompt_version,
            "generated_at": utc_now(),
            "input_revision_ids": [source["revision_id"]],
            "input_hashes": [source["content_hash"]],
            "parameters": dict(parameters),
            "disclosure_policy": dict(disclosure_policy) if disclosure_policy else None,
        }

    @staticmethod
    def _require_remote_allow(decision: Mapping[str, Any] | None) -> None:
        if not decision or decision.get("outcome") != "ALLOW":
            reason = (decision or {}).get("reason_codes", ["REMOTE_DISCLOSURE_NOT_AUTHORIZED"])
            raise ProductIngestError(
                "REMOTE_DISCLOSURE_NOT_AUTHORIZED",
                f"Model-backed extraction requires an ALLOW policy decision: {reason}",
            )


# ----------------------------------------------------------------------
# Deterministic directive parsing


def _iter_directives(content: str, conversation: bool) -> Iterable[dict[str, Any]]:
    encoded = content.encode("utf-8")
    offset = 0
    for raw_line in encoded.splitlines(keepends=True):
        line_without_newline = raw_line.rstrip(b"\r\n")
        if not line_without_newline:
            offset += len(raw_line)
            continue
        text = line_without_newline.decode("utf-8")
        candidates = [text]
        if conversation:
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                value = None
            extracted = _message_texts(value)
            if extracted:
                candidates = extracted
        for candidate in candidates:
            for candidate_line in candidate.splitlines():
                match = _DIRECTIVE.match(candidate_line)
                if not match:
                    continue
                yield {
                    "kind": match.group(1).upper(),
                    "payload": match.group(2).strip(),
                    "start_byte": offset,
                    "end_byte": offset + len(line_without_newline),
                    "excerpt": text,
                }
        offset += len(raw_line)


def _message_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        result: list[str] = []
        for key in ("content", "text", "message", "output", "input"):
            item = value.get(key)
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, list):
                result.extend(_message_texts(item))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_message_texts(item))
        return result
    return []


def _fact_operation(
    source: Mapping[str, Any],
    directive: Mapping[str, Any],
    predicate_by_key: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    fields = [part.strip() for part in str(directive["payload"]).split("|")]
    if len(fields) < 4:
        raise ProductIngestError(
            "FACT_DIRECTIVE_INVALID",
            "FACT requires subject | predicate | object | environment [| valid_from].",
        )
    subject_id, predicate_key, object_value, environment = fields[:4]
    predicate = predicate_by_key.get(predicate_key)
    if predicate is None:
        raise ProductIngestError("UNKNOWN_PREDICATE", predicate_key)
    subject_type = subject_id.split(":", 1)[0]
    if subject_type not in predicate["subject_types"]:
        raise ProductIngestError("SUBJECT_TYPE_NOT_ALLOWED", subject_type)
    object_definition = predicate["object"]
    if object_definition["kind"] == "entity":
        if ":" not in object_value:
            entity_type = object_definition["entity_types"][0]
            object_id = f"{entity_type}:{_semantic_component(object_value)}"
        else:
            object_id = object_value
            entity_type = object_id.split(":", 1)[0]
        object_document = {
            "kind": "entity",
            "entity_id": object_id,
            "entity_type": entity_type,
        }
    elif object_definition["kind"] == "enum":
        object_document = {"kind": "enum", "value": object_value.upper()}
    else:
        object_document = {"kind": "string", "value": object_value}
    captured_at = str(source["captured_at"])
    valid_from = _normalize_timestamp(fields[4]) if len(fields) > 4 else captured_at
    if predicate["temporal"] == "REQUIRED" and valid_from is None:
        valid_from = captured_at
    proposition = {
        "proposition_version": 1,
        "subject": {"entity_id": subject_id, "entity_type": subject_type},
        "predicate": predicate_key,
        "object": object_document,
        "polarity": "POSITIVE",
        "scope": {
            "component": None,
            "environment": environment or None,
            "region": None,
            "tenant": None,
        },
        "valid_time": {"from": valid_from, "to": None},
    }
    digest = sha256_json(
        {
            "revision_id": source["revision_id"],
            "start": directive["start_byte"],
            "proposition": proposition,
        }
    ).split(":", 1)[1]
    claim_id = f"claim_extract_{digest[:24]}"
    evidence_id = f"evidence_extract_{digest[:24]}"
    excerpt = str(directive["excerpt"])
    actor = {"actor_id": "service:shared-mind-extractor", "actor_type": "SERVICE"}
    return {
        "op_id": f"operation_assert_{digest[:24]}",
        "op": "ASSERT_CLAIM",
        "claim": {
            "object_type": "CLAIM",
            "claim_id": claim_id,
            "proposition_hash": sha256_json(proposition),
            "proposition": proposition,
            "asserted_by": actor,
            "asserted_at": captured_at,
        },
        "initial_evidence": [
            {
                "object_type": "EVIDENCE_LINK",
                "evidence_link_id": evidence_id,
                "claim_id": claim_id,
                "source_revision_id": source["revision_id"],
                "selector": {
                    "kind": "TEXT_BYTE_RANGE",
                    "start_byte": directive["start_byte"],
                    "end_byte": directive["end_byte"],
                    "excerpt": excerpt,
                    "excerpt_hash": sha256_bytes(excerpt.encode("utf-8")),
                    "prefix_hash": None,
                    "suffix_hash": None,
                },
                "stance": "SUPPORTS",
                "interpretation": "DIRECT",
                "linked_by": actor,
                "linked_at": captured_at,
            }
        ],
    }


def _decision_operation(source: Mapping[str, Any], directive: Mapping[str, Any]) -> dict[str, Any]:
    fields = [part.strip() for part in str(directive["payload"]).split("|")]
    if len(fields) < 2:
        title = fields[0]
        conclusion = fields[0]
        rationale = f"Extracted from {source['title']}."
        alternatives: list[str] = []
    else:
        title, conclusion = fields[:2]
        rationale = fields[2] if len(fields) > 2 and fields[2] else f"Extracted from {source['title']}."
        alternatives = [item.strip() for item in fields[3].split(";") if item.strip()] if len(fields) > 3 else []
    digest = _directive_digest(source, directive, "decision")
    actor = {"actor_id": "service:shared-mind-extractor", "actor_type": "SERVICE"}
    return {
        "op_id": f"operation_decision_{digest}",
        "op": "RECORD_DECISION",
        "decision": {
            "object_type": "DECISION_RECORD",
            "decision_id": f"decision_extract_{digest}",
            "title": title,
            "conclusion": conclusion,
            "rationale": rationale,
            "alternatives": alternatives,
            "related_source_revision_ids": [source["revision_id"]],
            "related_claim_ids": [],
            "status": "ACTIVE",
            "version": 1,
            "replaced_by_decision_id": None,
            "recorded_by": actor,
            "recorded_at": source["captured_at"],
        },
    }


def _question_operation(source: Mapping[str, Any], directive: Mapping[str, Any]) -> dict[str, Any]:
    fields = [part.strip() for part in str(directive["payload"]).split("|", 1)]
    question = fields[0]
    context = fields[1] if len(fields) > 1 and fields[1] else f"Extracted from {source['title']}."
    digest = _directive_digest(source, directive, "question")
    actor = {"actor_id": "service:shared-mind-extractor", "actor_type": "SERVICE"}
    return {
        "op_id": f"operation_question_{digest}",
        "op": "OPEN_QUESTION",
        "question": {
            "object_type": "OPEN_QUESTION",
            "question_id": f"question_extract_{digest}",
            "question": question,
            "context": context,
            "related_objects": [
                {"record_type": "SOURCE_REVISION", "record_id": source["revision_id"]}
            ],
            "status": "OPEN",
            "version": 1,
            "answer": None,
            "drop": None,
            "opened_by": actor,
            "opened_at": source["captured_at"],
        },
    }


def _work_operation(source: Mapping[str, Any], directive: Mapping[str, Any]) -> dict[str, Any]:
    fields = [part.strip() for part in str(directive["payload"]).split("|", 1)]
    priority = fields[0].upper() if len(fields) > 1 else "P2"
    description = fields[1] if len(fields) > 1 else fields[0]
    if priority not in {"P0", "P1", "P2", "P3"}:
        description = str(directive["payload"]).strip()
        priority = "P2"
    digest = _directive_digest(source, directive, "work")
    actor = {"actor_id": "service:shared-mind-extractor", "actor_type": "SERVICE"}
    return {
        "op_id": f"operation_work_{digest}",
        "op": "CREATE_WORK_ITEM",
        "work_item": {
            "object_type": "WORK_ITEM",
            "work_item_id": f"workitem_extract_{digest}",
            "description": description,
            "priority": priority,
            "blocker": None,
            "related_objects": [
                {"record_type": "SOURCE_REVISION", "record_id": source["revision_id"]}
            ],
            "status": "TODO",
            "version": 1,
            "created_by": actor,
            "created_at": source["captured_at"],
            "updated_at": source["captured_at"],
        },
    }


def _skill_candidate(source: Mapping[str, Any], directive: Mapping[str, Any]) -> dict[str, Any]:
    fields = [part.strip() for part in str(directive["payload"]).split("|")]
    purpose = fields[0]
    triggers = [item.strip() for item in fields[1].split(",") if item.strip()] if len(fields) > 1 else [purpose]
    steps = [item.strip() for item in fields[2].split(";") if item.strip()] if len(fields) > 2 else [purpose]
    validation_rules: list[dict[str, Any]] = []
    if len(fields) > 3:
        for item in fields[3].split(";"):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                kind, value = item.split(":", 1)
                validation_rules.append({"type": kind.strip().upper(), "value": value.strip()})
            else:
                validation_rules.append({"type": item.upper()})
    if not validation_rules:
        validation_rules = [{"type": "NON_EMPTY"}]
    return build_skill_record(
        skill_id=skill_id_from_purpose(purpose),
        version=1,
        purpose=purpose,
        triggers=triggers,
        steps=steps,
        validation_rules=validation_rules,
        status="DRAFT",
        provenance={
            "source_revision_id": source["revision_id"],
            "extractor": DETERMINISTIC_EXTRACTOR_VERSION,
        },
        created_at=source["captured_at"],
    )


def _directive_digest(source: Mapping[str, Any], directive: Mapping[str, Any], kind: str) -> str:
    return sha256_json(
        {
            "revision_id": source["revision_id"],
            "kind": kind,
            "start": directive["start_byte"],
            "payload": directive["payload"],
        }
    ).split(":", 1)[1][:24]


def _semantic_component(value: str) -> str:
    slug = _SEMANTIC_UNSAFE.sub("-", value.casefold()).strip("-._")
    return slug[:120] or sha256(value.encode("utf-8")).hexdigest()[:16]


def _normalize_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "DETERMINISTIC_EXTRACTOR_VERSION",
    "ExtractionLimits",
    "IngestManager",
    "ModelExtractor",
    "ProductIngestError",
]
