"""Thin WorkItem write helpers for agents that hand work between sessions.

Hand-assembling a WorkItem Proposal means restating the state root, six version
pins, the read set, and the guard pair on every call, which is where callers
drift from the kernel contract.  These helpers fill those fields from the live
workspace instead, so the only remaining input is the intent.  They are a
convenience layer, never a bypass: every mutation still enters through a
validated Proposal.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from .canonical import sha256_json
from .service import WorkspaceService
from .workspace import Workspace


WORK_ITEM_STATUSES = ("TODO", "DOING", "BLOCKED", "DONE", "DROPPED")
WORK_ITEM_PRIORITIES = ("P0", "P1", "P2", "P3")

_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}:[a-z0-9][a-z0-9._-]{0,127}$")
_RECORD_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_RECORD_TYPES = frozenset(
    {
        "SOURCE_REVISION",
        "CLAIM",
        "CONFLICT",
        "DECISION_RECORD",
        "OPEN_QUESTION",
        "WORK_ITEM",
    }
)

_MAX_DESCRIPTION = 8192
_MAX_RATIONALE = 4096
_MAX_BLOCKER = 4096

# One rebase covers the ordinary two-session race.  A second failure means the
# workspace is genuinely contended, and guessing again would risk clobbering a
# transition the caller never saw.
_MAX_REBASE_ATTEMPTS = 1


class WorkItemWriteError(RuntimeError):
    """A WorkItem write that the wrapper refused or the kernel did not accept."""

    def __init__(self, message: str, *, code: str = "WORK_ITEM_WRITE_FAILED",
                 details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def handoff(
    workspace: Workspace,
    description: str,
    *,
    actor: str,
    priority: str = "P1",
    related: Sequence[Mapping[str, str]] | None = None,
) -> str:
    """Create one TODO WorkItem and return its id."""

    description = _clean_text("description", description, _MAX_DESCRIPTION)
    actor_ref = _actor_ref(actor)
    if priority not in WORK_ITEM_PRIORITIES:
        raise WorkItemWriteError(
            f"priority must be one of {', '.join(WORK_ITEM_PRIORITIES)}; got {priority!r}",
            code="INVALID_WORK_ITEM_PRIORITY",
        )
    related_objects = _related_objects(related)

    service = WorkspaceService(workspace)
    # The id is derived from the same material as the idempotency key so a retried
    # handoff resolves to the existing item instead of creating a duplicate.
    fingerprint = sha256_json(
        {
            "actor": actor,
            "description": description,
            "kind": "CREATE_WORK_ITEM",
            "priority": priority,
            "related_objects": related_objects,
        }
    )[7:]
    work_item_id = f"workitem_handoff_{fingerprint[:32]}"

    existing = _find_work_item(service, work_item_id)
    if existing is not None:
        return work_item_id

    now = _now()
    proposal = {
        "object_type": "PROPOSAL",
        "proposal_id": f"proposal_handoff_{fingerprint[:32]}",
        "idempotency_key": f"handoff-{fingerprint[:48]}",
        "proposer": actor_ref,
        "proposed_at": now,
        "base_state_root": _state_root(workspace),
        "versions": service.current_version_bundle(),
        "reads": [],
        "guards": [],
        "operations": [
            {
                "op_id": f"operation_handoff_{fingerprint[:32]}",
                "op": "CREATE_WORK_ITEM",
                "work_item": {
                    "object_type": "WORK_ITEM",
                    "work_item_id": work_item_id,
                    "description": description,
                    "priority": priority,
                    "blocker": None,
                    "related_objects": related_objects,
                    "status": "TODO",
                    "version": 1,
                    "created_by": actor_ref,
                    "created_at": now,
                    "updated_at": now,
                },
            }
        ],
    }

    result = service.commit_proposal(proposal)
    if not result.ok:
        raise WorkItemWriteError(
            f"CREATE_WORK_ITEM was not committed ({result.code}).",
            code=result.code,
            details=result.errors or result.data,
        )
    return work_item_id


def progress(
    workspace: Workspace,
    work_item_id: str,
    status: str,
    rationale: str,
    *,
    actor: str,
    blocker: str | None = None,
    _on_rebase: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Move one WorkItem to ``status``, rebasing once on a transaction conflict.

    ``_on_rebase`` exists so tests can inject a competing write into the retry
    window; production callers never pass it.
    """

    if status not in WORK_ITEM_STATUSES:
        raise WorkItemWriteError(
            f"status must be one of {', '.join(WORK_ITEM_STATUSES)}; got {status!r}",
            code="INVALID_WORK_ITEM_STATUS",
        )
    if (status == "BLOCKED") != (blocker is not None):
        raise WorkItemWriteError(
            "blocker is required for BLOCKED and must be None for every other status.",
            code="INVALID_WORK_ITEM_BLOCKER",
        )
    if blocker is not None:
        blocker = _clean_text("blocker", blocker, _MAX_BLOCKER)
    rationale = _clean_text("rationale", rationale, _MAX_RATIONALE)
    actor_ref = _actor_ref(actor)
    if _RECORD_ID.fullmatch(work_item_id) is None:
        raise WorkItemWriteError(
            f"work_item_id is not a valid record id: {work_item_id!r}",
            code="INVALID_WORK_ITEM_ID",
        )

    service = WorkspaceService(workspace)
    attempt = 0
    while True:
        current = _find_work_item(service, work_item_id)
        if current is None:
            raise WorkItemWriteError(
                f"work item not found: {work_item_id}",
                code="WORK_ITEM_NOT_FOUND",
            )
        expected_version = int(current["version"])
        expected_status = str(current["status"])

        proposal = _status_proposal(
            service=service,
            workspace=workspace,
            work_item_id=work_item_id,
            status=status,
            rationale=rationale,
            blocker=blocker,
            actor_ref=actor_ref,
            expected_status=expected_status,
            expected_version=expected_version,
            attempt=attempt,
        )

        if _on_rebase is not None:
            _on_rebase()

        result = service.commit_proposal(proposal)
        if result.ok:
            return {
                "code": result.code,
                "work_item_id": work_item_id,
                "status": status,
                "version": expected_version + 1,
                "previous_status": expected_status,
                "blocker": blocker,
                "ledger_sequence": (result.data or {}).get("ledger_sequence"),
                "state_root": (result.data or {}).get("state_root"),
                "rebased": attempt > 0,
            }

        if result.code != "TRANSACTION_CONFLICT" or attempt >= _MAX_REBASE_ATTEMPTS:
            raise WorkItemWriteError(
                f"UPDATE_WORK_ITEM_STATUS was not committed ({result.code}).",
                code=result.code,
                details=result.errors or result.data,
            )
        attempt += 1


def list_work_items(
    workspace: Workspace, *, status: str | None = None
) -> list[dict[str, Any]]:
    """Return current WorkItem documents, optionally narrowed to one status."""

    if status is not None and status not in WORK_ITEM_STATUSES:
        raise WorkItemWriteError(
            f"status must be one of {', '.join(WORK_ITEM_STATUSES)}; got {status!r}",
            code="INVALID_WORK_ITEM_STATUS",
        )
    documents = _work_item_documents(WorkspaceService(workspace))
    if status is not None:
        documents = [item for item in documents if item.get("status") == status]
    return documents


def _status_proposal(
    *,
    service: WorkspaceService,
    workspace: Workspace,
    work_item_id: str,
    status: str,
    rationale: str,
    blocker: str | None,
    actor_ref: dict[str, str],
    expected_status: str,
    expected_version: int,
    attempt: int,
) -> dict[str, Any]:
    # The expected version is part of the key so a rebase is a distinct proposal
    # rather than an idempotent replay of the one that just lost.
    fingerprint = sha256_json(
        {
            "actor": actor_ref["actor_id"],
            "blocker": blocker,
            "expected_version": expected_version,
            "kind": "UPDATE_WORK_ITEM_STATUS",
            "rationale": rationale,
            "status": status,
            "work_item_id": work_item_id,
        }
    )[7:]
    return {
        "object_type": "PROPOSAL",
        "proposal_id": f"proposal_progress_{fingerprint[:32]}",
        "idempotency_key": f"progress-{fingerprint[:48]}",
        "proposer": actor_ref,
        "proposed_at": _now(),
        "base_state_root": _state_root(workspace),
        "versions": service.current_version_bundle(),
        "reads": [
            {
                "kind": "AGGREGATE",
                "aggregate_type": "WORK_ITEM",
                "aggregate_id": work_item_id,
                "expected_version": expected_version,
            }
        ],
        "guards": [
            {
                "op": "WORK_ITEM_STATUS_EQ",
                "work_item_id": work_item_id,
                "expected_status": expected_status,
            },
            {
                "op": "WORK_ITEM_VERSION_EQ",
                "work_item_id": work_item_id,
                "expected_version": expected_version,
            },
        ],
        "operations": [
            {
                "op_id": f"operation_progress_{fingerprint[:32]}",
                "op": "UPDATE_WORK_ITEM_STATUS",
                "target_work_item_id": work_item_id,
                "new_status": status,
                "blocker": blocker,
                "rationale": rationale,
                "updated_by": actor_ref,
                "updated_at": _now(),
            }
        ],
    }


def _work_item_documents(service: WorkspaceService) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = service.query(
            {"kinds": ["WORK_ITEM"], "limit": 1000, "offset": offset}
        )
        if not result.ok:
            raise WorkItemWriteError(
                f"WorkItem query failed ({result.code}).",
                code=result.code,
                details=result.message,
            )
        data = result.data or {}
        for hit in data.get("hits", ()):
            record = hit.get("record") or {}
            document = record.get("document")
            if isinstance(document, dict):
                documents.append(document)
        if not data.get("truncated"):
            break
        offset += len(data.get("hits", ()))
    return documents


def _find_work_item(
    service: WorkspaceService, work_item_id: str
) -> dict[str, Any] | None:
    for document in _work_item_documents(service):
        if document.get("work_item_id") == work_item_id:
            return document
    return None


def _state_root(workspace: Workspace) -> str:
    kernel = workspace.open_kernel()
    try:
        return kernel.state_root()
    finally:
        kernel.close()


def _actor_ref(actor: str) -> dict[str, str]:
    if not isinstance(actor, str) or _SEMANTIC_ID.fullmatch(actor) is None:
        raise WorkItemWriteError(
            f"actor must be a semantic id such as 'agent:discord-bot'; got {actor!r}",
            code="INVALID_ACTOR_ID",
        )
    return {"actor_id": actor, "actor_type": "AGENT"}


def _related_objects(
    related: Sequence[Mapping[str, str]] | None,
) -> list[dict[str, str]]:
    if related is None:
        return []
    if isinstance(related, (str, bytes)) or not isinstance(related, Sequence):
        raise WorkItemWriteError(
            "related must be a sequence of {record_type, record_id} mappings.",
            code="INVALID_RELATED_OBJECTS",
        )
    normalized: list[dict[str, str]] = []
    for entry in related:
        if not isinstance(entry, Mapping):
            raise WorkItemWriteError(
                "related entries must be mappings with record_type and record_id.",
                code="INVALID_RELATED_OBJECTS",
            )
        record_type = entry.get("record_type")
        record_id = entry.get("record_id")
        if record_type not in _RECORD_TYPES:
            raise WorkItemWriteError(
                f"related record_type must be one of {', '.join(sorted(_RECORD_TYPES))};"
                f" got {record_type!r}",
                code="INVALID_RELATED_OBJECTS",
            )
        if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
            raise WorkItemWriteError(
                f"related record_id is not a valid record id: {record_id!r}",
                code="INVALID_RELATED_OBJECTS",
            )
        candidate = {"record_type": record_type, "record_id": record_id}
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _clean_text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkItemWriteError(
            f"{name} must be a non-empty string.",
            code="INVALID_WORK_ITEM_TEXT",
        )
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise WorkItemWriteError(
            f"{name} must be at most {maximum} characters.",
            code="INVALID_WORK_ITEM_TEXT",
        )
    return cleaned


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "WORK_ITEM_PRIORITIES",
    "WORK_ITEM_STATUSES",
    "WorkItemWriteError",
    "handoff",
    "list_work_items",
    "progress",
]
