from __future__ import annotations

import base64
import json
import os
import re
import sysconfig
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, sha256_bytes
from .kernel import Kernel, Receipt
from .validation import build_contract_validator, load_default_schema


WORKSPACE_DIRECTORY = ".shared-mind"
CONFIG_FILENAME = "workspace.json"
DATABASE_FILENAME = "shared-mind.sqlite3"
REGISTRY_FILENAME = "predicate-registry.json"
WORKSPACE_VERSION = 1

_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}:[a-z0-9][a-z0-9._-]{0,127}$")
_SAFE_SLUG = re.compile(r"[^a-z0-9._-]+")


class WorkspaceError(Exception):
    """A stable, user-correctable workspace failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    object_path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "object_path": self.object_path,
            "message": self.message,
        }


@dataclass(frozen=True)
class Workspace:
    """Resolved paths and safe operations for one local Shared Mind workspace."""

    root: Path
    control_root: Path
    config_path: Path
    database_path: Path
    registry_path: Path
    source_root: Path
    projection_root: Path
    blob_root: Path

    @classmethod
    def initialize(
        cls,
        root: str | Path,
        *,
        registry_source: str | Path | None = None,
    ) -> "Workspace":
        root_path = Path(root).expanduser().resolve()
        if root_path.exists() and not root_path.is_dir():
            raise WorkspaceError(
                "WORKSPACE_PATH_NOT_DIRECTORY",
                f"Workspace path is not a directory: {root_path}",
            )
        root_path.mkdir(parents=True, exist_ok=True)
        control_root = root_path / WORKSPACE_DIRECTORY
        control_root.mkdir(exist_ok=True)
        (root_path / "sources").mkdir(exist_ok=True)
        (root_path / "projections").mkdir(exist_ok=True)
        (control_root / "blobs").mkdir(exist_ok=True)

        config_path = control_root / CONFIG_FILENAME
        config = {
            "blob_root": f"{WORKSPACE_DIRECTORY}/blobs",
            "database": f"{WORKSPACE_DIRECTORY}/{DATABASE_FILENAME}",
            "projection_root": "projections",
            "registry": f"{WORKSPACE_DIRECTORY}/{REGISTRY_FILENAME}",
            "source_root": "sources",
            "workspace_version": WORKSPACE_VERSION,
        }
        encoded_config = (canonical_json(config) + "\n").encode("utf-8")
        if config_path.exists():
            if not config_path.is_file():
                raise WorkspaceError(
                    "WORKSPACE_CONFIG_INVALID",
                    f"Workspace config is not a regular file: {config_path}",
                )
            try:
                existing_config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkspaceError(
                    "WORKSPACE_CONFIG_INVALID", f"Cannot read workspace config: {exc}"
                ) from exc
            if existing_config != config:
                raise WorkspaceError(
                    "WORKSPACE_CONFIG_CONFLICT",
                    "The existing workspace config does not match workspace version 1.",
                )
        else:
            cls._write_new_file(config_path, encoded_config)

        registry_path = control_root / REGISTRY_FILENAME
        registry_bytes = cls._load_registry_bytes(registry_source)
        if registry_path.exists():
            try:
                existing_registry = registry_path.read_bytes()
            except OSError as exc:
                raise WorkspaceError(
                    "REGISTRY_READ_FAILED", f"Cannot read predicate registry: {exc}"
                ) from exc
            if existing_registry != registry_bytes:
                raise WorkspaceError(
                    "REGISTRY_CONFLICT",
                    "The existing workspace predicate registry differs from the requested registry.",
                )
        else:
            cls._write_new_file(registry_path, registry_bytes)

        workspace = cls.open(root_path)
        kernel = workspace.open_kernel()
        kernel.close()
        return workspace

    @classmethod
    def open(cls, start: str | Path) -> "Workspace":
        start_path = Path(start).expanduser().resolve()
        candidate = start_path if start_path.is_dir() else start_path.parent
        config_path: Path | None = None
        for directory in (candidate, *candidate.parents):
            possible = directory / WORKSPACE_DIRECTORY / CONFIG_FILENAME
            if possible.is_file():
                config_path = possible
                break
        if config_path is None:
            raise WorkspaceError(
                "WORKSPACE_NOT_FOUND",
                f"No {WORKSPACE_DIRECTORY}/{CONFIG_FILENAME} found from {start_path}",
            )
        root = config_path.parent.parent.resolve()
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "WORKSPACE_CONFIG_INVALID", f"Cannot read workspace config: {exc}"
            ) from exc
        if config.get("workspace_version") != WORKSPACE_VERSION:
            raise WorkspaceError(
                "UNSUPPORTED_WORKSPACE_VERSION",
                f"Unsupported workspace version: {config.get('workspace_version')!r}",
            )
        required = ("database", "registry", "source_root", "projection_root", "blob_root")
        missing = [name for name in required if not isinstance(config.get(name), str)]
        if missing:
            raise WorkspaceError(
                "WORKSPACE_CONFIG_INVALID",
                f"Workspace config is missing string fields: {', '.join(missing)}",
            )
        resolved = {
            name: cls._resolve_configured_path(root, config[name], name)
            for name in required
        }
        return cls(
            root=root,
            control_root=config_path.parent,
            config_path=config_path,
            database_path=resolved["database"],
            registry_path=resolved["registry"],
            source_root=resolved["source_root"],
            projection_root=resolved["projection_root"],
            blob_root=resolved["blob_root"],
        )

    def describe(self) -> dict[str, str | int]:
        return {
            "workspace_version": WORKSPACE_VERSION,
            "workspace": str(self.root),
            "config": self._relative(self.config_path),
            "database": self._relative(self.database_path),
            "registry": self._relative(self.registry_path),
            "source_root": self._relative(self.source_root),
            "projection_root": self._relative(self.projection_root),
            "blob_root": self._relative(self.blob_root),
        }

    def open_kernel(self) -> Kernel:
        try:
            registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "REGISTRY_READ_FAILED", f"Cannot read predicate registry: {exc}"
            ) from exc
        return Kernel(self.database_path, registry)

    def resolve_source_input(self, source: str | Path) -> Path:
        requested = Path(source).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        resolved = requested.resolve()
        source_root = self.source_root.resolve()
        if not self._is_within(resolved, source_root):
            raise WorkspaceError(
                "PATH_OUTSIDE_SOURCE_ROOT",
                f"Source path must be inside {source_root}: {resolved}",
            )
        if not resolved.is_file():
            raise WorkspaceError("SOURCE_NOT_FOUND", f"Source file not found: {resolved}")
        return resolved

    def add_source(
        self,
        source_path: str | Path,
        *,
        source_id: str | None = None,
    ) -> tuple[dict[str, Any], Receipt]:
        path = self.resolve_source_input(source_path)
        media_type = self._media_type(path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise WorkspaceError("SOURCE_READ_FAILED", f"Cannot read source: {exc}") from exc
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                "SOURCE_NOT_UTF8", f"Source must contain valid UTF-8 text: {exc}"
            ) from exc
        effective_source_id = source_id or self._default_source_id(path)
        if not _SEMANTIC_ID.fullmatch(effective_source_id):
            raise WorkspaceError(
                "INVALID_SOURCE_ID",
                f"Source id does not match the semantic id contract: {effective_source_id}",
            )
        content_hash = sha256_bytes(content)
        identity_hash = sha256(
            effective_source_id.encode("utf-8") + b"\0" + content
        ).hexdigest()
        revision_id = f"revision_{identity_hash[:32]}"
        blob_name = content_hash.split(":", 1)[1]
        blob_path = self.blob_root / blob_name
        self._preserve_blob(blob_path, content, content_hash)

        kernel = self.open_kernel()
        try:
            existing = kernel.connection.execute(
                "SELECT document FROM sources WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if existing:
                source_revision = json.loads(existing["document"])
            else:
                source_revision = {
                    "object_type": "SOURCE_REVISION",
                    "source_id": effective_source_id,
                    "revision_id": revision_id,
                    "content_hash": content_hash,
                    "blob_ref": (
                        f"data:{media_type};base64,"
                        + base64.b64encode(content).decode("ascii")
                    ),
                    "source_locator": path.as_uri(),
                    "title": path.name,
                    "media_type": media_type,
                    "captured_at": datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "registered_by": {
                        "actor_id": "service:shared-mind-cli",
                        "actor_type": "SERVICE",
                    },
                }

            proposal = self._source_proposal(source_revision, identity_hash)
            receipt = kernel.commit(proposal)
            return (
                {
                    "source_id": effective_source_id,
                    "revision_id": revision_id,
                    "content_hash": content_hash,
                    "media_type": media_type,
                    "blob_path": self._relative(blob_path),
                },
                receipt,
            )
        finally:
            kernel.close()

    def validate_proposal(self, proposal: Any) -> list[ValidationIssue]:
        contract = load_default_schema()
        proposal_contract = {
            "$schema": contract["$schema"],
            "$defs": contract["$defs"],
            "$ref": "#/$defs/Proposal",
        }
        validator = build_contract_validator(proposal_contract)
        schema_errors = sorted(
            validator.iter_errors(proposal),
            key=lambda error: (tuple(str(item) for item in error.absolute_path), error.message),
        )
        if schema_errors:
            return [
                ValidationIssue(
                    "SCHEMA_VALIDATION_FAILED",
                    self._json_path(error.absolute_path),
                    error.message,
                )
                for error in schema_errors
            ]
        assert isinstance(proposal, dict)
        versions = proposal["versions"]
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        supported = {
            "schema": (Kernel.SUPPORTED_VERSIONS["schema"], "UNSUPPORTED_SCHEMA_VERSION"),
            "predicate_registry": (
                registry["version"],
                "UNSUPPORTED_PREDICATE_REGISTRY",
            ),
            "conflict_rules": (
                Kernel.SUPPORTED_VERSIONS["conflict_rules"],
                "UNSUPPORTED_CONFLICT_RULES_VERSION",
            ),
            "guard_dsl": (
                registry["guard_dsl_version"],
                "UNSUPPORTED_GUARD_DSL_VERSION",
            ),
            "projection": (
                Kernel.SUPPORTED_VERSIONS["projection"],
                "UNSUPPORTED_PROJECTION_VERSION",
            ),
        }
        return [
            ValidationIssue(
                code,
                f"$.versions.{name}",
                f"Unsupported {name} version {versions[name]!r}; expected {expected!r}.",
            )
            for name, (expected, code) in supported.items()
            if versions[name] != expected
        ]

    def load_json(self, path: str | Path) -> Any:
        requested = Path(path).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        resolved = requested.resolve()
        if not resolved.is_file():
            raise WorkspaceError("FILE_NOT_FOUND", f"JSON file not found: {resolved}")
        try:
            return json.loads(resolved.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkspaceError(
                "MALFORMED_JSON",
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            ) from exc
        except UnicodeDecodeError as exc:
            raise WorkspaceError("MALFORMED_JSON", f"JSON must be UTF-8: {exc}") from exc
        except OSError as exc:
            raise WorkspaceError("FILE_READ_FAILED", f"Cannot read JSON file: {exc}") from exc

    def list_conflicts(self, status: str | None = None) -> list[dict[str, Any]]:
        kernel = self.open_kernel()
        try:
            if status is None:
                rows = kernel.connection.execute(
                    "SELECT * FROM conflicts ORDER BY conflict_id"
                ).fetchall()
            else:
                rows = kernel.connection.execute(
                    "SELECT * FROM conflicts WHERE status = ? ORDER BY conflict_id",
                    (status,),
                ).fetchall()
            conflicts = []
            for row in rows:
                item = dict(row)
                for field in ("members", "resolution", "document"):
                    if field in item and isinstance(item[field], str):
                        try:
                            item[field] = json.loads(item[field])
                        except json.JSONDecodeError:
                            pass
                conflicts.append(item)
            return conflicts
        finally:
            kernel.close()

    def write_projection(self, filename: str, content: str) -> Path:
        if Path(filename).name != filename:
            raise WorkspaceError("INVALID_PROJECTION_NAME", "Invalid projection filename.")
        self.projection_root.mkdir(parents=True, exist_ok=True)
        destination = self.projection_root / filename
        temporary = self.projection_root / f".{filename}.tmp-{os.getpid()}"
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, destination)
        return destination

    def _source_proposal(
        self, source_revision: dict[str, Any], identity_hash: str
    ) -> dict[str, Any]:
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        suffix = identity_hash[:32]
        return {
            "object_type": "PROPOSAL",
            "proposal_id": f"proposal_register_{suffix}",
            "idempotency_key": f"source-register:{identity_hash[:48]}",
            "proposer": {
                "actor_id": "service:shared-mind-cli",
                "actor_type": "SERVICE",
            },
            "proposed_at": source_revision["captured_at"],
            "base_state_root": None,
            "versions": {
                "schema": Kernel.SUPPORTED_VERSIONS["schema"],
                "predicate_registry": registry["version"],
                "conflict_rules": Kernel.SUPPORTED_VERSIONS["conflict_rules"],
                "guard_dsl": registry["guard_dsl_version"],
                "projection": Kernel.SUPPORTED_VERSIONS["projection"],
            },
            "reads": [],
            "guards": [],
            "operations": [
                {
                    "op_id": f"operation_register_{suffix}",
                    "op": "REGISTER_SOURCE_REVISION",
                    "source_revision": source_revision,
                }
            ],
        }

    def _preserve_blob(self, path: Path, content: bytes, expected_hash: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise WorkspaceError("BLOB_READ_FAILED", f"Cannot verify source blob: {exc}") from exc
            if sha256_bytes(existing) != expected_hash:
                raise WorkspaceError(
                    "BLOB_INTEGRITY_ERROR",
                    f"Content-addressed blob does not match its filename: {path}",
                )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    @staticmethod
    def _default_source_id(path: Path) -> str:
        slug = _SAFE_SLUG.sub("-", path.stem.lower()).strip("-._") or "source"
        return f"document:{slug[:120]}"

    @staticmethod
    def _media_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in (".md", ".markdown"):
            return "text/markdown"
        if suffix == ".txt":
            return "text/plain"
        raise WorkspaceError(
            "UNSUPPORTED_SOURCE_MEDIA_TYPE",
            f"Only Markdown and UTF-8 text files are supported: {path.name}",
        )

    @staticmethod
    def _json_path(parts: Iterable[Any]) -> str:
        result = "$"
        for part in parts:
            result += f"[{part}]" if isinstance(part, int) else f".{part}"
        return result

    @staticmethod
    def _is_within(path: Path, directory: Path) -> bool:
        try:
            path.relative_to(directory)
            return True
        except ValueError:
            return False

    @staticmethod
    def _resolve_configured_path(root: Path, value: str, field: str) -> Path:
        configured = Path(value)
        if configured.is_absolute():
            raise WorkspaceError(
                "WORKSPACE_CONFIG_INVALID", f"{field} must be a relative path."
            )
        resolved = (root / configured).resolve()
        if not Workspace._is_within(resolved, root):
            raise WorkspaceError(
                "WORKSPACE_CONFIG_INVALID", f"{field} escapes the workspace root."
            )
        return resolved

    @staticmethod
    def _write_new_file(path: Path, content: bytes) -> None:
        try:
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            return
        except OSError as exc:
            raise WorkspaceError("WORKSPACE_WRITE_FAILED", f"Cannot write {path}: {exc}") from exc

    @staticmethod
    def _load_registry_bytes(registry_source: str | Path | None) -> bytes:
        if registry_source is not None:
            candidates = (Path(registry_source).expanduser().resolve(),)
        else:
            candidates = (
                Path(__file__).resolve().parents[2]
                / "contracts"
                / "atlas-predicate-registry.v1.json",
                Path(sysconfig.get_path("data"))
                / "share"
                / "shared-mind"
                / "contracts"
                / "atlas-predicate-registry.v1.json",
            )
        for candidate in candidates:
            if candidate.is_file():
                try:
                    document = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise WorkspaceError(
                        "REGISTRY_READ_FAILED", f"Cannot read predicate registry: {exc}"
                    ) from exc
                return (canonical_json(document) + "\n").encode("utf-8")
        raise WorkspaceError(
            "REGISTRY_NOT_FOUND",
            "The default predicate registry is unavailable; provide a packaged registry.",
        )
