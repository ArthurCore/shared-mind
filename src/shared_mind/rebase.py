"""Read-only advisory hints for rebuilding stale proposals.

Rebase hints deliberately remain outside the canonical commit path.  They
describe the current values of optimistic-concurrency preconditions, but never
rewrite a proposal or imply that applying those values would be safe.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import sha256_json
from .kernel import Kernel, Receipt


_CONTINUITY_TARGETS = {
    "DECISION_RECORD": ("decision_records", "decision_id"),
    "OPEN_QUESTION": ("open_questions", "question_id"),
    "WORK_ITEM": ("work_items", "work_item_id"),
}

_VERSION_REASONS = {
    "CLAIM": "CLAIM_VERSION_MISMATCH",
    "CONFLICT": "CONFLICT_VERSION_MISMATCH",
    "DECISION_RECORD": "DECISION_VERSION_MISMATCH",
    "OPEN_QUESTION": "QUESTION_VERSION_MISMATCH",
    "WORK_ITEM": "WORK_ITEM_VERSION_MISMATCH",
}

_GUARD_TARGETS = {
    "CLAIM_STATUS_EQ": (
        "CLAIM",
        "claim_id",
        "status",
        "expected_status",
        "CLAIM_STATUS_MISMATCH",
    ),
    "CLAIM_VERSION_EQ": (
        "CLAIM",
        "claim_id",
        "version",
        "expected_version",
        "CLAIM_VERSION_MISMATCH",
    ),
    "CONFLICT_STATUS_EQ": (
        "CONFLICT",
        "conflict_id",
        "status",
        "expected_status",
        "CONFLICT_STATUS_MISMATCH",
    ),
    "CONFLICT_MEMBER_DIGEST_EQ": (
        "CONFLICT",
        "conflict_id",
        "member_digest",
        "expected_digest",
        "CONFLICT_MEMBER_DIGEST_MISMATCH",
    ),
    "DECISION_STATUS_EQ": (
        "DECISION_RECORD",
        "decision_id",
        "status",
        "expected_status",
        "DECISION_STATUS_MISMATCH",
    ),
    "DECISION_VERSION_EQ": (
        "DECISION_RECORD",
        "decision_id",
        "version",
        "expected_version",
        "DECISION_VERSION_MISMATCH",
    ),
    "QUESTION_STATUS_EQ": (
        "OPEN_QUESTION",
        "question_id",
        "status",
        "expected_status",
        "QUESTION_STATUS_MISMATCH",
    ),
    "QUESTION_VERSION_EQ": (
        "OPEN_QUESTION",
        "question_id",
        "version",
        "expected_version",
        "QUESTION_VERSION_MISMATCH",
    ),
    "WORK_ITEM_STATUS_EQ": (
        "WORK_ITEM",
        "work_item_id",
        "status",
        "expected_status",
        "WORK_ITEM_STATUS_MISMATCH",
    ),
    "WORK_ITEM_VERSION_EQ": (
        "WORK_ITEM",
        "work_item_id",
        "version",
        "expected_version",
        "WORK_ITEM_VERSION_MISMATCH",
    ),
}


@dataclass(frozen=True)
class _Precondition:
    path: str
    expected: Any
    actual: Any
    aggregate_type: str
    aggregate_id: str
    actual_state: dict[str, Any]
    reason_code: str


def build_rebase_hint(
    kernel: Kernel,
    proposal: Mapping[str, Any],
    receipt: Receipt,
) -> dict[str, Any] | None:
    """Build a non-authoritative hint for one rejected stale proposal.

    Accepted proposals, fact conflicts, validation failures, and transaction
    conflicts whose precondition cannot be interpreted safely return ``None``.
    All observations use Kernel's public, read-only surfaces.
    """

    if receipt.outcome != "TRANSACTION_CONFLICT" or not receipt.reason_codes:
        return None
    if not isinstance(receipt.document, dict):
        return None

    reason_code = receipt.reason_codes[0]
    preconditions = _proposal_preconditions(kernel, proposal)
    failed = next(
        (
            item
            for item in preconditions
            if item.reason_code == reason_code and item.expected != item.actual
        ),
        None,
    )
    if failed is None:
        return None

    replacements = []
    seen_paths: set[str] = set()
    for item in preconditions:
        if (
            item.aggregate_type == failed.aggregate_type
            and item.aggregate_id == failed.aggregate_id
            and item.path not in seen_paths
        ):
            replacements.append({"path": item.path, "value": item.actual})
            seen_paths.add(item.path)

    entries = kernel.ledger_entries()
    observed_head = entries[-1]["entry_hash"] if entries else None
    return {
        "hint_version": "rebase-hint@1",
        "advisory": True,
        "proposal_id": proposal.get("proposal_id"),
        "receipt_id": receipt.document.get("receipt_id"),
        "reason_code": reason_code,
        "observed_state_root": kernel.state_root(),
        "observed_ledger_head": observed_head,
        "failed_precondition": {
            "path": failed.path,
            "expected": failed.expected,
            "actual": failed.actual,
            "aggregate_type": failed.aggregate_type,
            "aggregate_id": failed.aggregate_id,
            "actual_state": failed.actual_state,
        },
        "replacement_preconditions": replacements,
        "safe_to_auto_apply": False,
        "recommended_action": "REVIEW_AND_REBUILD",
    }


def _proposal_preconditions(
    kernel: Kernel, proposal: Mapping[str, Any]
) -> list[_Precondition]:
    result: list[_Precondition] = []

    for index, read in enumerate(proposal.get("reads", ())):
        if not isinstance(read, Mapping):
            continue
        if read.get("kind") == "COLLECTION":
            family_key = read.get("family_key")
            if not isinstance(family_key, str):
                continue
            state = _active_collection_state(kernel, family_key)
            result.append(
                _Precondition(
                    path=f"$.reads[{index}].expected_digest",
                    expected=read.get("expected_digest"),
                    actual=state["digest"],
                    aggregate_type="CLAIM_COLLECTION",
                    aggregate_id=family_key,
                    actual_state=state,
                    reason_code="ACTIVE_SET_DIGEST_MISMATCH",
                )
            )
            continue
        if read.get("kind") != "AGGREGATE":
            continue
        aggregate_type = read.get("aggregate_type")
        aggregate_id = read.get("aggregate_id")
        if (
            not isinstance(aggregate_type, str)
            or not isinstance(aggregate_id, str)
            or aggregate_type not in _VERSION_REASONS
        ):
            continue
        state = _aggregate_state(kernel, aggregate_type, aggregate_id)
        result.append(
            _Precondition(
                path=f"$.reads[{index}].expected_version",
                expected=read.get("expected_version"),
                actual=state["version"],
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                actual_state=state,
                reason_code=_VERSION_REASONS[aggregate_type],
            )
        )

    for index, guard in enumerate(proposal.get("guards", ())):
        if not isinstance(guard, Mapping):
            continue
        if guard.get("op") == "ACTIVE_SET_DIGEST_EQ":
            family_key = guard.get("family_key")
            if not isinstance(family_key, str):
                continue
            state = _active_collection_state(kernel, family_key)
            result.append(
                _Precondition(
                    path=f"$.guards[{index}].expected_digest",
                    expected=guard.get("expected_digest"),
                    actual=state["digest"],
                    aggregate_type="CLAIM_COLLECTION",
                    aggregate_id=family_key,
                    actual_state=state,
                    reason_code="ACTIVE_SET_DIGEST_MISMATCH",
                )
            )
            continue
        target = _GUARD_TARGETS.get(guard.get("op"))
        if target is None:
            continue
        (
            aggregate_type,
            id_field,
            state_field,
            expected_field,
            reason_code,
        ) = target
        aggregate_id = guard.get(id_field)
        if not isinstance(aggregate_id, str):
            continue
        state = _aggregate_state(kernel, aggregate_type, aggregate_id)
        result.append(
            _Precondition(
                path=f"$.guards[{index}].{expected_field}",
                expected=guard.get(expected_field),
                actual=state[state_field],
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                actual_state=state,
                reason_code=reason_code,
            )
        )

    for index, operation in enumerate(proposal.get("operations", ())):
        if not isinstance(operation, Mapping):
            continue
        if operation.get("op") != "RESOLVE_CONFLICT":
            continue
        conflict_id = operation.get("conflict_id")
        if not isinstance(conflict_id, str):
            continue
        state = _aggregate_state(kernel, "CONFLICT", conflict_id)
        result.append(
            _Precondition(
                path=f"$.operations[{index}].expected_member_digest",
                expected=operation.get("expected_member_digest"),
                actual=state["member_digest"],
                aggregate_type="CONFLICT",
                aggregate_id=conflict_id,
                actual_state=state,
                reason_code="CONFLICT_MEMBER_DIGEST_MISMATCH",
            )
        )
        resolution = operation.get("resolution")
        if isinstance(resolution, Mapping):
            result.append(
                _Precondition(
                    path=f"$.operations[{index}].resolution.resolution_epoch",
                    expected=resolution.get("resolution_epoch"),
                    actual=_conflict_episode(kernel, conflict_id),
                    aggregate_type="CONFLICT",
                    aggregate_id=conflict_id,
                    actual_state=state,
                    reason_code="CONFLICT_RESOLUTION_EPOCH_MISMATCH",
                )
            )

    return result


def _aggregate_state(
    kernel: Kernel, aggregate_type: str, aggregate_id: str
) -> dict[str, Any]:
    if aggregate_type == "CLAIM":
        row = kernel.connection.execute(
            "SELECT version, status FROM claims WHERE claim_id = ?",
            (aggregate_id,),
        ).fetchone()
        return {
            "version": None if row is None else int(row["version"]),
            "status": None if row is None else row["status"],
        }
    if aggregate_type == "CONFLICT":
        row = kernel.connection.execute(
            "SELECT version, status, member_digest "
            "FROM conflicts WHERE conflict_id = ?",
            (aggregate_id,),
        ).fetchone()
        return {
            "version": None if row is None else int(row["version"]),
            "status": None if row is None else row["status"],
            "member_digest": None if row is None else row["member_digest"],
        }

    table, id_column = _CONTINUITY_TARGETS[aggregate_type]
    row = kernel.connection.execute(
        f"SELECT version, status FROM {table} WHERE {id_column} = ?",
        (aggregate_id,),
    ).fetchone()
    return {
        "version": None if row is None else int(row["version"]),
        "status": None if row is None else row["status"],
    }


def _conflict_episode(kernel: Kernel, conflict_id: str) -> int | None:
    row = kernel.connection.execute(
        "SELECT episode FROM conflicts WHERE conflict_id = ?", (conflict_id,)
    ).fetchone()
    return None if row is None else int(row["episode"])


def _active_collection_state(kernel: Kernel, family_key: str) -> dict[str, Any]:
    predicates = {item["key"]: item for item in kernel.registry["predicates"]}
    members: list[dict[str, Any]] = []
    for row in kernel.connection.execute(
        "SELECT claim_id, proposition_hash, proposition, version "
        "FROM claims WHERE status = 'ACTIVE' ORDER BY claim_id"
    ):
        proposition = json.loads(row["proposition"])
        predicate = predicates.get(proposition.get("predicate"))
        if predicate is None or _family_key(proposition, predicate) != family_key:
            continue
        members.append(
            {
                "claim_id": row["claim_id"],
                "proposition_hash": row["proposition_hash"],
                "version": int(row["version"]),
            }
        )
    return {"digest": sha256_json(members), "member_count": len(members)}


def _family_key(
    proposition: Mapping[str, Any], predicate: Mapping[str, Any]
) -> str:
    values = []
    for path in predicate["family_key_fields"]:
        value: Any = proposition
        for part in path.split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
        values.append(value)
    return sha256_json(values)
