"""Idempotent project and Codex integration setup for Shared Mind."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import sysconfig
import tempfile
from collections.abc import Mapping
from hashlib import sha256
from math import ceil
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .product import ProductError, ProductService
from .workspace import CONFIG_FILENAME, WORKSPACE_DIRECTORY, Workspace, WorkspaceError


SETUP_VERSION = "natural-language-setup@1"
SETUP_SKILL_NAME = "shared-mind-setup"
DEFAULT_SETUP_TASK = "Continue the highest-priority unblocked project work."
DEFAULT_SETUP_BUDGET_BYTES = 24 * 1024
MAX_SETUP_BUDGET_BYTES = 128 * 1024
_SKILL_FILES = ("SKILL.md", "agents/openai.yaml")


def setup_project(
    *,
    start: str | Path,
    project: str | Path | None = None,
    workspace_path: str | Path | None = None,
    purpose: str | None = None,
    cold_start: bool = True,
    install_codex_skill: bool = True,
    install_claude_hooks: bool = False,
) -> dict[str, Any]:
    """Set up one project workspace and return verified resumable context."""

    project_root = _resolve_project_root(start, explicit=project)
    skill_plan = (
        _codex_skill_plan()
        if install_codex_skill
        else {"status": "SKIPPED", "path": None, "content_hash": None}
    )
    settings = (
        _read_claude_settings(project_root)
        if install_claude_hooks
        else None
    )
    workspace, workspace_created = _resolve_workspace(
        project_root,
        explicit=workspace_path,
        purpose=purpose,
    )
    skill = _apply_codex_skill_plan(skill_plan) if install_codex_skill else skill_plan
    hooks = (
        _install_claude_hooks(project_root, workspace, settings)
        if install_claude_hooks
        else {"status": "SKIPPED", "path": None}
    )

    service = ProductService(workspace)
    try:
        cold_start_completed = service.store.has_audit_event("COLD_START_COMPLETED")
        if cold_start and not cold_start_completed:
            report = _cold_start_with_setup_budget(service, project_root)
            cold_start_result = {
                "performed": True,
                "batch_id": report["batch_id"],
                "ingest": report["ingest"],
                "extraction": report["extraction"],
                "committed": report["committed"],
                "review_queue": report["review_queue"],
                "unresolved_conflicts": report["unresolved_conflicts"],
            }
        else:
            cold_start_result = {
                "performed": False,
                "reason": "ALREADY_COMPLETED" if cold_start_completed else "DISABLED",
            }

        integrity = service.verify()
        if _only_disposable_views_are_stale(integrity):
            repair = service.incremental_consolidation()
            consolidation = {
                "performed": True,
                "changed_artifact_ids": repair["changed_artifact_ids"],
            }
            integrity = service.verify()
        else:
            consolidation = {
                "performed": False,
                "changed_artifact_ids": [],
            }
        if not integrity["valid"]:
            raise ProductError(
                "PRODUCT_INTEGRITY_INVALID",
                "Product verification failed during Shared Mind setup.",
                data=integrity,
            )
        context = _setup_context(service)
    finally:
        service.close()

    return {
        "setup_version": SETUP_VERSION,
        "project": project_root.as_posix(),
        "workspace": workspace.root.as_posix(),
        "workspace_created": workspace_created,
        "cold_start": cold_start_result,
        "consolidation": consolidation,
        "codex_skill": skill,
        "claude_hooks": hooks,
        "integrity": integrity,
        "context": context,
    }


def _only_disposable_views_are_stale(integrity: dict[str, Any]) -> bool:
    return bool(
        not integrity.get("valid")
        and integrity.get("kernel", {}).get("valid")
        and integrity.get("product_audit", {}).get("valid")
        and integrity.get("skill_replay", {}).get("valid")
        and not integrity.get("artifact_provenance_issues")
        and not integrity.get("derived_views", {}).get("valid")
    )


def _setup_context(service: ProductService) -> dict[str, Any]:
    budget = DEFAULT_SETUP_BUDGET_BYTES
    request: dict[str, Any] = {
        "task": DEFAULT_SETUP_TASK,
        "purpose": None,
        "query": (
            "project purpose current decisions open questions active work "
            "conflicts evidence"
        ),
        "references": [],
        "depth": "EVIDENCE",
        "budget_bytes": budget,
        "budget_tokens": None,
        "hints": {},
    }
    while True:
        request["budget_bytes"] = budget
        try:
            return service.context(request)
        except ProductError as exc:
            budget = _next_setup_budget(exc, current=budget)


def _cold_start_with_setup_budget(
    service: ProductService, project_root: Path
) -> dict[str, Any]:
    budget = DEFAULT_SETUP_BUDGET_BYTES
    while True:
        try:
            return service.cold_start(
                [project_root],
                task=DEFAULT_SETUP_TASK,
                budget_bytes=budget,
            )
        except ProductError as exc:
            budget = _next_setup_budget(exc, current=budget)


def _next_setup_budget(exc: ProductError, *, current: int) -> int:
    if exc.code != "CONTEXT_BUDGET_TOO_SMALL" or not isinstance(exc.data, Mapping):
        raise exc
    required = exc.data.get("required_bytes")
    if isinstance(required, bool) or not isinstance(required, int) or required <= 0:
        raise exc
    if "mandatory purpose, continuity" in exc.message:
        next_budget = ceil(required / 0.72)
    else:
        next_budget = required
    if next_budget <= current or next_budget > MAX_SETUP_BUDGET_BYTES:
        raise exc
    return next_budget


def _resolve_project_root(
    start: str | Path, *, explicit: str | Path | None
) -> Path:
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceError(
                "PROJECT_ROOT_INVALID", f"Project path is not a directory: {root}"
            )
        return root

    start_path = Path(start).expanduser().resolve()
    candidate = start_path if start_path.is_dir() else start_path.parent
    for directory in (candidate, *candidate.parents):
        git_marker = directory / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return directory
    raise WorkspaceError(
        "PROJECT_ROOT_NOT_FOUND",
        f"No Git project root found from {start_path}; pass --project explicitly.",
    )


def _resolve_workspace(
    project_root: Path,
    *,
    explicit: str | Path | None,
    purpose: str | None,
) -> tuple[Workspace, bool]:
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        config = root / WORKSPACE_DIRECTORY / CONFIG_FILENAME
        if config.is_file():
            return Workspace.open(root), False
        return Workspace.initialize(
            root,
            purpose=purpose or _default_purpose(project_root),
        ), True

    try:
        return Workspace.discover(project_root), False
    except WorkspaceError as exc:
        if exc.code != "WORKSPACE_NOT_FOUND":
            raise
    root = project_root.parent / f"{project_root.name}-memory"
    return Workspace.initialize(
        root,
        purpose=purpose or _default_purpose(project_root),
    ), True


def _default_purpose(project_root: Path) -> str:
    return f"Preserve {project_root.name} project state across AI sessions."


def _read_claude_settings(project_root: Path) -> dict[str, Any] | None:
    claude_root = project_root / ".claude"
    settings_path = claude_root / "settings.json"
    if claude_root.is_symlink() or settings_path.is_symlink():
        raise WorkspaceError(
            "CLAUDE_HOOKS_PATH_INVALID",
            "Claude settings path must not be a symbolic link.",
        )
    if claude_root.exists() and not claude_root.is_dir():
        raise WorkspaceError(
            "CLAUDE_HOOKS_PATH_INVALID", f"Claude config path is not a directory: {claude_root}"
        )
    if not settings_path.exists():
        return None
    if not settings_path.is_file():
        raise WorkspaceError(
            "CLAUDE_HOOKS_PATH_INVALID",
            f"Claude settings path is not a regular file: {settings_path}",
        )
    try:
        document = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(
            "CLAUDE_HOOKS_SETTINGS_INVALID", f"Cannot read Claude settings: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise WorkspaceError(
            "CLAUDE_HOOKS_SETTINGS_INVALID", "Claude settings must be a JSON object."
        )
    hooks = document.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise WorkspaceError(
            "CLAUDE_HOOKS_SETTINGS_INVALID", "Claude settings hooks must be a JSON object."
        )
    return document


def _install_claude_hooks(
    project_root: Path,
    workspace: Workspace,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    settings_path = project_root / ".claude" / "settings.json"
    document = dict(existing) if existing is not None else {}
    hooks = dict(document.get("hooks", {}))
    executable = shlex.quote(sys.executable)
    module = "shared_mind.adapters.claude_code_hooks"
    workspace_argument = shlex.quote(workspace.root.as_posix())
    commands = {
        "PostToolUse": f"{executable} -m {module} append --workspace {workspace_argument}",
        "SessionEnd": f"{executable} -m {module} finalize --workspace {workspace_argument}",
        "Stop": f"{executable} -m {module} finalize --workspace {workspace_argument}",
    }
    changed = False
    for event_name, command in commands.items():
        entries = hooks.get(event_name, [])
        if not isinstance(entries, list):
            raise WorkspaceError(
                "CLAUDE_HOOKS_SETTINGS_INVALID",
                f"Claude hook {event_name} must be an array.",
            )
        entry = {
            "matcher": "",
            "hooks": [{"type": "command", "command": command}],
        }
        if entry not in entries:
            hooks[event_name] = [*entries, entry]
            changed = True
    if not changed:
        return {"status": "UNCHANGED", "path": settings_path.as_posix()}
    document["hooks"] = hooks
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    content = (canonical_json(document) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=settings_path.parent,
            prefix=".settings-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, settings_path)
        temporary_path = None
    except OSError as exc:
        raise WorkspaceError("CLAUDE_HOOKS_INSTALL_FAILED", str(exc)) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return {
        "status": "UPDATED" if existing is not None else "INSTALLED",
        "path": settings_path.as_posix(),
    }


def _codex_skill_plan() -> dict[str, Any]:
    source = _skill_source()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    skills_root = codex_home.expanduser().resolve() / "skills"
    destination = skills_root / SETUP_SKILL_NAME
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise WorkspaceError(
            "CODEX_SKILL_CONFLICT",
            f"Codex skill destination is not a managed directory: {destination}",
        )
    digest = _skill_digest(source)
    if destination.exists():
        if all(
            (destination / relative).is_file()
            and (destination / relative).read_bytes() == (source / relative).read_bytes()
            for relative in _SKILL_FILES
        ):
            return {
                "status": "UNCHANGED",
                "path": destination.as_posix(),
                "content_hash": digest,
                "source": source,
                "destination": destination,
            }
        raise WorkspaceError(
            "CODEX_SKILL_CONFLICT",
            "An unmanaged or modified shared-mind-setup skill already exists at "
            f"{destination}; it was not overwritten.",
        )
    return {
        "status": "INSTALLED",
        "path": destination.as_posix(),
        "content_hash": digest,
        "source": source,
        "destination": destination,
    }


def _apply_codex_skill_plan(plan: dict[str, Any]) -> dict[str, Any]:
    source = plan["source"]
    destination = plan["destination"]
    if plan["status"] == "INSTALLED":
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{SETUP_SKILL_NAME}-", dir=destination.parent)
        )
        try:
            for relative in _SKILL_FILES:
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((source / relative).read_bytes())
            try:
                os.replace(temporary, destination)
            except OSError:
                if destination.exists() and all(
                    (destination / relative).is_file()
                    and (destination / relative).read_bytes()
                    == (source / relative).read_bytes()
                    for relative in _SKILL_FILES
                ):
                    shutil.rmtree(temporary)
                    plan["status"] = "UNCHANGED"
                else:
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {
        "status": plan["status"],
        "path": plan["path"],
        "content_hash": plan["content_hash"],
    }


def _skill_source() -> Path:
    candidates = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "shared-mind"
        / "skills"
        / SETUP_SKILL_NAME,
        Path(__file__).resolve().parents[2]
        / ".agents"
        / "skills"
        / SETUP_SKILL_NAME,
    )
    for candidate in candidates:
        if all((candidate / relative).is_file() for relative in _SKILL_FILES):
            return candidate
    raise WorkspaceError(
        "CODEX_SKILL_SOURCE_MISSING",
        "The installed Shared Mind package does not contain the Codex setup skill.",
    )


def _skill_digest(root: Path) -> str:
    digest = sha256()
    for relative in _SKILL_FILES:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
