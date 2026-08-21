"""Project-scoped automatic session bootstrap for hook-capable AI hosts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .canonical import canonical_json, sha256_bytes
from .product import ProductError, ProductService
from .workspace import CONFIG_FILENAME, WORKSPACE_DIRECTORY, Workspace, WorkspaceError


BINDING_VERSION = "project-binding@1"
BINDING_FILENAME = "project-binding.json"
SESSION_BOOTSTRAP_VERSION = "session-bootstrap@1"
DEFAULT_BOOTSTRAP_TASK = "Continue the highest-priority unblocked project work."
DEFAULT_BOOTSTRAP_QUERY = (
    "project purpose current decisions open questions active work conflicts evidence"
)
DEFAULT_BOOTSTRAP_BUDGET_BYTES = 24 * 1024


def binding_path_for(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / WORKSPACE_DIRECTORY / BINDING_FILENAME


def workspace_config_hash(workspace_root: str | Path) -> str:
    config_path = (
        Path(workspace_root).expanduser().resolve()
        / WORKSPACE_DIRECTORY
        / CONFIG_FILENAME
    )
    return sha256_bytes(_read_regular_file(config_path, "WORKSPACE_CONFIG_INVALID"))


def build_project_binding(project_root: str | Path, workspace: Workspace) -> dict[str, str]:
    project = Path(project_root).expanduser().resolve()
    return {
        "binding_version": BINDING_VERSION,
        "project_root": project.as_posix(),
        "workspace_root": workspace.root.resolve().as_posix(),
        "workspace_config_hash": workspace_config_hash(workspace.root),
    }


def write_project_binding(project_root: str | Path, workspace: Workspace) -> Path:
    path = binding_path_for(project_root)
    if path.is_symlink():
        raise WorkspaceError(
            "PROJECT_BINDING_PATH_INVALID",
            "Project binding path must not be a symbolic link.",
        )
    if path.parent.is_symlink():
        raise WorkspaceError(
            "PROJECT_BINDING_PATH_INVALID",
            "Project binding control directory must not be a symbolic link.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise WorkspaceError(
            "PROJECT_BINDING_PATH_INVALID",
            f"Project binding path is not a regular file: {path}",
        )
    content = (canonical_json(build_project_binding(project_root, workspace)) + "\n").encode(
        "utf-8"
    )
    if path.exists() and path.read_bytes() == content:
        return path
    _atomic_replace(path, content)
    return path


def bootstrap_session(
    *,
    cwd: str | Path | None = None,
    prompt: str | None = None,
    phase: str = "SessionStart",
    binding: str | Path | None = None,
    budget_bytes: int = DEFAULT_BOOTSTRAP_BUDGET_BYTES,
) -> dict[str, Any]:
    start = Path(cwd).expanduser() if cwd is not None else Path.cwd()
    try:
        project_root = _nearest_git_root(start)
        binding_path = (
            Path(binding).expanduser().resolve()
            if binding is not None
            else binding_path_for(project_root)
        )
        document, binding_hash = _read_binding(binding_path)
        _validate_binding_document(document, project_root)
        workspace_root = Path(document["workspace_root"]).expanduser().resolve()
        expected_hash = str(document["workspace_config_hash"])
        actual_hash = workspace_config_hash(workspace_root)
        if actual_hash != expected_hash:
            return _skipped(
                "WORKSPACE_BINDING_MISMATCH",
                phase=phase,
                project_root=project_root,
                workspace_root=workspace_root,
                binding_hash=binding_hash,
            )
        workspace = _open_exact_workspace(workspace_root)
        service = ProductService(workspace)
        try:
            integrity = service.verify()
            if not integrity["valid"]:
                return _skipped(
                    "PRODUCT_INTEGRITY_INVALID",
                    phase=phase,
                    project_root=project_root,
                    workspace_root=workspace.root,
                    binding_hash=binding_hash,
                    integrity=integrity,
                )
            context = service.context(_context_request(prompt, budget_bytes))
        finally:
            service.close()
    except WorkspaceError as exc:
        return _skipped(exc.code, phase=phase, warning=exc.message)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        return _skipped("PROJECT_BINDING_INVALID", phase=phase, warning=str(exc))
    except ProductError as exc:
        return _skipped(exc.code, phase=phase, warning=exc.message)

    additional_context = _additional_context(
        project_root=project_root,
        workspace_root=workspace.root,
        binding_hash=binding_hash,
        context=context,
    )
    return {
        "bootstrap_version": SESSION_BOOTSTRAP_VERSION,
        "status": "READY",
        "phase": phase,
        "project_root": project_root.as_posix(),
        "workspace_root": workspace.root.as_posix(),
        "binding_hash": binding_hash,
        "integrity_valid": True,
        "context_hash": context["context_hash"],
        "context": context,
        "additional_context": additional_context,
        "warning": None,
    }


def _nearest_git_root(start: Path) -> Path:
    start_path = start.expanduser().resolve()
    candidate = start_path if start_path.is_dir() else start_path.parent
    for directory in (candidate, *candidate.parents):
        git_marker = directory / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return directory.resolve()
    raise WorkspaceError(
        "PROJECT_ROOT_NOT_FOUND",
        f"No Git project root found from {start_path}.",
    )


def _read_binding(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        raise WorkspaceError(
            "PROJECT_BINDING_NOT_FOUND",
            f"No project binding found at {path}.",
        )
    content = _read_regular_file(path, "PROJECT_BINDING_INVALID")
    parsed = json.loads(content.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise WorkspaceError("PROJECT_BINDING_INVALID", "Project binding must be an object.")
    return parsed, sha256_bytes(content)


def _validate_binding_document(document: Mapping[str, Any], project_root: Path) -> None:
    if document.get("binding_version") != BINDING_VERSION:
        raise WorkspaceError(
            "PROJECT_BINDING_INVALID",
            f"Unsupported project binding version: {document.get('binding_version')!r}.",
        )
    project = document.get("project_root")
    workspace = document.get("workspace_root")
    config_hash = document.get("workspace_config_hash")
    if not isinstance(project, str) or not isinstance(workspace, str):
        raise WorkspaceError(
            "PROJECT_BINDING_INVALID",
            "Project binding requires project_root and workspace_root strings.",
        )
    if not isinstance(config_hash, str) or not config_hash.startswith("sha256:"):
        raise WorkspaceError(
            "PROJECT_BINDING_INVALID",
            "Project binding requires a sha256 workspace_config_hash.",
        )
    if Path(project).expanduser().resolve() != project_root.resolve():
        raise WorkspaceError(
            "PROJECT_BINDING_MISMATCH",
            "Project binding root does not match the current Git root.",
        )


def _open_exact_workspace(workspace_root: Path) -> Workspace:
    config_path = workspace_root / WORKSPACE_DIRECTORY / CONFIG_FILENAME
    _read_regular_file(config_path, "WORKSPACE_CONFIG_INVALID")
    workspace = Workspace.open(workspace_root)
    if workspace.root.resolve() != workspace_root.resolve():
        raise WorkspaceError(
            "WORKSPACE_BINDING_MISMATCH",
            "Workspace binding did not open the exact configured workspace root.",
        )
    return workspace


def _read_regular_file(path: Path, code: str) -> bytes:
    if path.is_symlink():
        raise WorkspaceError(code, f"Path must not be a symbolic link: {path}")
    if path.parent.is_symlink():
        raise WorkspaceError(code, f"Parent directory must not be a symbolic link: {path.parent}")
    if not path.is_file():
        raise WorkspaceError(code, f"Path is not a regular file: {path}")
    return path.read_bytes()


def _context_request(prompt: str | None, budget_bytes: int) -> dict[str, Any]:
    task = prompt.strip() if isinstance(prompt, str) and prompt.strip() else DEFAULT_BOOTSTRAP_TASK
    query = (
        f"{DEFAULT_BOOTSTRAP_QUERY} {task}"
        if task != DEFAULT_BOOTSTRAP_TASK
        else DEFAULT_BOOTSTRAP_QUERY
    )
    return {
        "task": task,
        "purpose": None,
        "query": query,
        "references": [],
        "depth": "EVIDENCE",
        "budget_bytes": budget_bytes,
        "budget_tokens": None,
        "hints": {},
    }


def _additional_context(
    *,
    project_root: Path,
    workspace_root: Path,
    binding_hash: str,
    context: Mapping[str, Any],
) -> str:
    envelope = {
        "bootstrap_version": SESSION_BOOTSTRAP_VERSION,
        "project_root": project_root.as_posix(),
        "workspace_root": workspace_root.as_posix(),
        "binding_hash": binding_hash,
        "context_hash": context["context_hash"],
        "context": context,
    }
    return "Shared Mind automatic project context:\n" + canonical_json(envelope)


def _skipped(
    status: str,
    *,
    phase: str,
    warning: str | None = None,
    project_root: Path | None = None,
    workspace_root: Path | None = None,
    binding_hash: str | None = None,
    integrity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "bootstrap_version": SESSION_BOOTSTRAP_VERSION,
        "status": status,
        "phase": phase,
        "project_root": project_root.as_posix() if project_root is not None else None,
        "workspace_root": workspace_root.as_posix() if workspace_root is not None else None,
        "binding_hash": binding_hash,
        "integrity_valid": bool(integrity and integrity.get("valid")),
        "context_hash": None,
        "context": None,
        "additional_context": None,
        "warning": warning or status,
    }


def _atomic_replace(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
