"""Deterministic reducers for decisions, questions, and work items.

The functions in this module deliberately do not own transactions.  The
kernel validates a proposal and opens its write transaction before calling
these reducers, so materialized continuity state and the ledger entry can be
committed or rolled back as one unit.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, MutableSequence

from .canonical import canonical_json


CONTINUITY_OPERATION_TYPES = frozenset(
    {
        "RECORD_DECISION",
        "SUPERSEDE_DECISION",
        "OPEN_QUESTION",
        "ANSWER_QUESTION",
        "DROP_QUESTION",
        "CREATE_WORK_ITEM",
        "UPDATE_WORK_ITEM_STATUS",
    }
)


@dataclass(frozen=True)
class RequiredRead:
    """Aggregate identity that a destructive operation must pin."""

    aggregate_type: str
    aggregate_id: str


class ContinuityError(Exception):
    """Base class carrying a stable receipt reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ContinuityValidationError(ContinuityError):
    """The operation is structurally valid but violates domain semantics."""


class ContinuityConflict(ContinuityError):
    """The operation's lifecycle precondition no longer matches current state."""


_READ_TARGETS = {
    "DECISION_RECORD": ("decision_records", "decision_id", "DECISION"),
    "OPEN_QUESTION": ("open_questions", "question_id", "QUESTION"),
    "WORK_ITEM": ("work_items", "work_item_id", "WORK_ITEM"),
}

_GUARD_TARGETS = {
    "DECISION_STATUS_EQ": (
        "decision_records",
        "decision_id",
        "decision_id",
        "status",
        "expected_status",
        "DECISION_STATUS_MISMATCH",
    ),
    "DECISION_VERSION_EQ": (
        "decision_records",
        "decision_id",
        "decision_id",
        "version",
        "expected_version",
        "DECISION_VERSION_MISMATCH",
    ),
    "QUESTION_STATUS_EQ": (
        "open_questions",
        "question_id",
        "question_id",
        "status",
        "expected_status",
        "QUESTION_STATUS_MISMATCH",
    ),
    "QUESTION_VERSION_EQ": (
        "open_questions",
        "question_id",
        "question_id",
        "version",
        "expected_version",
        "QUESTION_VERSION_MISMATCH",
    ),
    "WORK_ITEM_STATUS_EQ": (
        "work_items",
        "work_item_id",
        "work_item_id",
        "status",
        "expected_status",
        "WORK_ITEM_STATUS_MISMATCH",
    ),
    "WORK_ITEM_VERSION_EQ": (
        "work_items",
        "work_item_id",
        "work_item_id",
        "version",
        "expected_version",
        "WORK_ITEM_VERSION_MISMATCH",
    ),
}

_WORK_ITEM_STATUSES = frozenset({"TODO", "DOING", "BLOCKED", "DONE", "DROPPED"})


def create_schema(connection: sqlite3.Connection) -> None:
    """Create continuity materializations without committing the caller's work."""

    statements = (
        """
        CREATE TABLE IF NOT EXISTS decision_records (
          decision_id TEXT PRIMARY KEY,
          status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'REVERSED')),
          version INTEGER NOT NULL CHECK (version >= 1),
          replaced_by_decision_id TEXT,
          document TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS open_questions (
          question_id TEXT PRIMARY KEY,
          status TEXT NOT NULL CHECK (status IN ('OPEN', 'ANSWERED', 'DROPPED')),
          version INTEGER NOT NULL CHECK (version >= 1),
          answer TEXT,
          drop_record TEXT,
          document TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS work_items (
          work_item_id TEXT PRIMARY KEY,
          status TEXT NOT NULL CHECK (status IN ('TODO', 'DOING', 'BLOCKED', 'DONE', 'DROPPED')),
          version INTEGER NOT NULL CHECK (version >= 1),
          blocker TEXT,
          updated_at TEXT NOT NULL,
          document TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)


def required_reads(operation: Mapping[str, Any]) -> tuple[RequiredRead, ...]:
    """Derive mandatory optimistic-concurrency reads from an operation."""

    kind = operation.get("op")
    if kind == "SUPERSEDE_DECISION":
        return (
            RequiredRead("DECISION_RECORD", operation["target_decision_id"]),
        )
    if kind in {"ANSWER_QUESTION", "DROP_QUESTION"}:
        return (RequiredRead("OPEN_QUESTION", operation["target_question_id"]),)
    if kind == "UPDATE_WORK_ITEM_STATUS":
        return (RequiredRead("WORK_ITEM", operation["target_work_item_id"]),)
    return ()


def validate_read(connection: sqlite3.Connection, read: Mapping[str, Any]) -> bool:
    """Validate one continuity aggregate read, returning whether it was handled."""

    if read.get("kind") != "AGGREGATE":
        return False
    target = _READ_TARGETS.get(read.get("aggregate_type"))
    if target is None:
        return False
    table, id_column, code_prefix = target
    row = _fetch_one(
        connection,
        f"SELECT version FROM {table} WHERE {id_column} = ?",
        (read["aggregate_id"],),
    )
    if row is None or row["version"] != read["expected_version"]:
        raise ContinuityConflict(f"{code_prefix}_VERSION_MISMATCH")
    return True


def validate_guard(connection: sqlite3.Connection, guard: Mapping[str, Any]) -> bool:
    """Validate one continuity lifecycle guard, returning whether it was handled."""

    target = _GUARD_TARGETS.get(guard.get("op"))
    if target is None:
        return False
    table, id_column, guard_id, state_column, expected_field, error_code = target
    row = _fetch_one(
        connection,
        f"SELECT {state_column} FROM {table} WHERE {id_column} = ?",
        (guard[guard_id],),
    )
    if row is None or row[state_column] != guard[expected_field]:
        raise ContinuityConflict(error_code)
    return True


def apply_operation(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    events: MutableSequence[dict[str, Any]],
) -> bool:
    """Apply a continuity operation and append its replay-complete event.

    ``False`` means that the operation belongs to another kernel reducer.  No
    branch starts, commits, or rolls back a transaction.
    """

    kind = operation.get("op")
    if kind not in CONTINUITY_OPERATION_TYPES:
        return False
    if kind == "RECORD_DECISION":
        _record_decision(connection, operation["decision"], events)
    elif kind == "SUPERSEDE_DECISION":
        _supersede_decision(connection, operation, events)
    elif kind == "OPEN_QUESTION":
        _open_question(connection, operation["question"], events)
    elif kind == "ANSWER_QUESTION":
        _answer_question(connection, operation, events)
    elif kind == "DROP_QUESTION":
        _drop_question(connection, operation, events)
    elif kind == "CREATE_WORK_ITEM":
        _create_work_item(connection, operation["work_item"], events)
    else:
        _update_work_item_status(connection, operation, events)
    return True


def apply_event(connection: sqlite3.Connection, event: Mapping[str, Any]) -> bool:
    """Replay one continuity event and verify its transition metadata.

    Reducers regenerate the event from the pre-event materialized state.  An
    exact comparison prevents a tampered previous/new version or status from
    being silently accepted during replay.
    """

    event_type = event.get("event_type")
    generated: list[dict[str, Any]] = []
    if event_type == "DECISION_RECORDED":
        _record_decision(connection, event["decision"], generated)
    elif event_type == "DECISION_SUPERSEDED":
        _supersede_decision(
            connection,
            {
                "target_decision_id": event["target_decision_id"],
                "target_disposition": event["target_disposition"],
                "replacement_decision": event["replacement_decision"],
                "rationale": event["rationale"],
            },
            generated,
        )
    elif event_type == "QUESTION_OPENED":
        _open_question(connection, event["question"], generated)
    elif event_type == "QUESTION_ANSWERED":
        _answer_question(
            connection,
            {
                "target_question_id": event["target_question_id"],
                "answer": event["answer"],
            },
            generated,
        )
    elif event_type == "QUESTION_DROPPED":
        _drop_question(
            connection,
            {
                "target_question_id": event["target_question_id"],
                "drop": event["drop"],
            },
            generated,
        )
    elif event_type == "WORK_ITEM_CREATED":
        _create_work_item(connection, event["work_item"], generated)
    elif event_type == "WORK_ITEM_STATUS_UPDATED":
        _update_work_item_status(
            connection,
            {
                "target_work_item_id": event["target_work_item_id"],
                "new_status": event["new_status"],
                "blocker": event["blocker"],
                "rationale": event["rationale"],
                "updated_by": event["updated_by"],
                "updated_at": event["updated_at"],
            },
            generated,
        )
    else:
        return False
    if generated != [dict(event)]:
        raise ContinuityValidationError("REPLAY_EVENT_TRANSITION_MISMATCH")
    return True


def state_rows(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Return JSON-safe continuity state in stable table and primary-key order."""

    decisions = _fetch_all(
        connection,
        """
        SELECT decision_id, status, version, replaced_by_decision_id, document
        FROM decision_records ORDER BY decision_id
        """,
    )
    questions = _fetch_all(
        connection,
        """
        SELECT question_id, status, version, answer, drop_record, document
        FROM open_questions ORDER BY question_id
        """,
    )
    work_items = _fetch_all(
        connection,
        """
        SELECT work_item_id, status, version, blocker, updated_at, document
        FROM work_items ORDER BY work_item_id
        """,
    )
    for row in decisions:
        row["document"] = json.loads(row["document"])
    for row in questions:
        row["answer"] = _load_optional_json(row["answer"])
        row["drop"] = _load_optional_json(row.pop("drop_record"))
        row["document"] = json.loads(row["document"])
    for row in work_items:
        row["document"] = json.loads(row["document"])
    return {
        "decision_records": decisions,
        "open_questions": questions,
        "work_items": work_items,
    }


def _record_decision(
    connection: sqlite3.Connection,
    decision: Mapping[str, Any],
    events: MutableSequence[dict[str, Any]],
) -> None:
    _validate_new_decision(decision)
    _ensure_available(
        connection, "decision_records", "decision_id", decision["decision_id"]
    )
    snapshot = copy.deepcopy(dict(decision))
    connection.execute(
        "INSERT INTO decision_records VALUES (?, ?, ?, ?, ?)",
        (
            snapshot["decision_id"],
            snapshot["status"],
            snapshot["version"],
            snapshot["replaced_by_decision_id"],
            canonical_json(snapshot),
        ),
    )
    events.append({"event_type": "DECISION_RECORDED", "decision": snapshot})


def _supersede_decision(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    events: MutableSequence[dict[str, Any]],
) -> None:
    if operation["target_disposition"] not in {"SUPERSEDED", "REVERSED"}:
        raise ContinuityValidationError("INVALID_DECISION_DISPOSITION")
    target_id = operation["target_decision_id"]
    target = _fetch_one(
        connection,
        "SELECT status, version, document FROM decision_records WHERE decision_id = ?",
        (target_id,),
    )
    if target is None or target["status"] != "ACTIVE":
        raise ContinuityConflict("DECISION_STATUS_MISMATCH")
    replacement = operation["replacement_decision"]
    _validate_new_decision(replacement)
    _ensure_available(
        connection, "decision_records", "decision_id", replacement["decision_id"]
    )
    replacement_snapshot = copy.deepcopy(dict(replacement))
    previous_version = int(target["version"])
    new_version = previous_version + 1
    target_document = json.loads(target["document"])
    target_document.update(
        {
            "status": operation["target_disposition"],
            "version": new_version,
            "replaced_by_decision_id": replacement_snapshot["decision_id"],
        }
    )
    connection.execute(
        "INSERT INTO decision_records VALUES (?, ?, ?, ?, ?)",
        (
            replacement_snapshot["decision_id"],
            replacement_snapshot["status"],
            replacement_snapshot["version"],
            replacement_snapshot["replaced_by_decision_id"],
            canonical_json(replacement_snapshot),
        ),
    )
    connection.execute(
        """
        UPDATE decision_records
        SET status = ?, version = ?, replaced_by_decision_id = ?, document = ?
        WHERE decision_id = ?
        """,
        (
            operation["target_disposition"],
            new_version,
            replacement_snapshot["decision_id"],
            canonical_json(target_document),
            target_id,
        ),
    )
    events.append(
        {
            "event_type": "DECISION_SUPERSEDED",
            "target_decision_id": target_id,
            "target_disposition": operation["target_disposition"],
            "replacement_decision": replacement_snapshot,
            "rationale": operation["rationale"],
            "previous_version": previous_version,
            "new_version": new_version,
        }
    )


def _open_question(
    connection: sqlite3.Connection,
    question: Mapping[str, Any],
    events: MutableSequence[dict[str, Any]],
) -> None:
    _validate_new_question(question)
    _ensure_available(
        connection, "open_questions", "question_id", question["question_id"]
    )
    snapshot = copy.deepcopy(dict(question))
    connection.execute(
        "INSERT INTO open_questions VALUES (?, ?, ?, ?, ?, ?)",
        (
            snapshot["question_id"],
            snapshot["status"],
            snapshot["version"],
            None,
            None,
            canonical_json(snapshot),
        ),
    )
    events.append({"event_type": "QUESTION_OPENED", "question": snapshot})


def _answer_question(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    events: MutableSequence[dict[str, Any]],
) -> None:
    target_id = operation["target_question_id"]
    target = _open_question_target(connection, target_id)
    answer = copy.deepcopy(dict(operation["answer"]))
    previous_version = int(target["version"])
    new_version = previous_version + 1
    document = json.loads(target["document"])
    document.update(
        {"status": "ANSWERED", "version": new_version, "answer": answer, "drop": None}
    )
    connection.execute(
        """
        UPDATE open_questions
        SET status = 'ANSWERED', version = ?, answer = ?, drop_record = NULL,
            document = ?
        WHERE question_id = ?
        """,
        (new_version, canonical_json(answer), canonical_json(document), target_id),
    )
    events.append(
        {
            "event_type": "QUESTION_ANSWERED",
            "target_question_id": target_id,
            "answer": answer,
            "previous_version": previous_version,
            "new_version": new_version,
        }
    )


def _drop_question(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    events: MutableSequence[dict[str, Any]],
) -> None:
    target_id = operation["target_question_id"]
    target = _open_question_target(connection, target_id)
    drop = copy.deepcopy(dict(operation["drop"]))
    previous_version = int(target["version"])
    new_version = previous_version + 1
    document = json.loads(target["document"])
    document.update(
        {"status": "DROPPED", "version": new_version, "answer": None, "drop": drop}
    )
    connection.execute(
        """
        UPDATE open_questions
        SET status = 'DROPPED', version = ?, answer = NULL, drop_record = ?,
            document = ?
        WHERE question_id = ?
        """,
        (new_version, canonical_json(drop), canonical_json(document), target_id),
    )
    events.append(
        {
            "event_type": "QUESTION_DROPPED",
            "target_question_id": target_id,
            "drop": drop,
            "previous_version": previous_version,
            "new_version": new_version,
        }
    )


def _create_work_item(
    connection: sqlite3.Connection,
    work_item: Mapping[str, Any],
    events: MutableSequence[dict[str, Any]],
) -> None:
    _validate_new_work_item(work_item)
    _ensure_available(
        connection, "work_items", "work_item_id", work_item["work_item_id"]
    )
    snapshot = copy.deepcopy(dict(work_item))
    connection.execute(
        "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?)",
        (
            snapshot["work_item_id"],
            snapshot["status"],
            snapshot["version"],
            snapshot["blocker"],
            snapshot["updated_at"],
            canonical_json(snapshot),
        ),
    )
    events.append({"event_type": "WORK_ITEM_CREATED", "work_item": snapshot})


def _update_work_item_status(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    events: MutableSequence[dict[str, Any]],
) -> None:
    new_status = operation["new_status"]
    blocker = operation["blocker"]
    if new_status not in _WORK_ITEM_STATUSES:
        raise ContinuityValidationError("INVALID_WORK_ITEM_STATUS")
    if (new_status == "BLOCKED") != (isinstance(blocker, str) and bool(blocker)):
        raise ContinuityValidationError("INVALID_WORK_ITEM_BLOCKER")
    target_id = operation["target_work_item_id"]
    target = _fetch_one(
        connection,
        "SELECT status, version, document FROM work_items WHERE work_item_id = ?",
        (target_id,),
    )
    if target is None:
        raise ContinuityConflict("WORK_ITEM_STATUS_MISMATCH")
    previous_status = target["status"]
    previous_version = int(target["version"])
    new_version = previous_version + 1
    document = json.loads(target["document"])
    document.update(
        {
            "status": new_status,
            "version": new_version,
            "blocker": blocker,
            "updated_at": operation["updated_at"],
        }
    )
    connection.execute(
        """
        UPDATE work_items
        SET status = ?, version = ?, blocker = ?, updated_at = ?, document = ?
        WHERE work_item_id = ?
        """,
        (
            new_status,
            new_version,
            blocker,
            operation["updated_at"],
            canonical_json(document),
            target_id,
        ),
    )
    events.append(
        {
            "event_type": "WORK_ITEM_STATUS_UPDATED",
            "target_work_item_id": target_id,
            "previous_status": previous_status,
            "new_status": new_status,
            "blocker": blocker,
            "rationale": operation["rationale"],
            "updated_by": copy.deepcopy(operation["updated_by"]),
            "updated_at": operation["updated_at"],
            "previous_version": previous_version,
            "new_version": new_version,
        }
    )


def _validate_new_decision(decision: Mapping[str, Any]) -> None:
    if (
        decision.get("status") != "ACTIVE"
        or decision.get("version") != 1
        or decision.get("replaced_by_decision_id") is not None
    ):
        raise ContinuityValidationError("INVALID_NEW_DECISION_STATE")


def _validate_new_question(question: Mapping[str, Any]) -> None:
    if (
        question.get("status") != "OPEN"
        or question.get("version") != 1
        or question.get("answer") is not None
        or question.get("drop") is not None
    ):
        raise ContinuityValidationError("INVALID_NEW_QUESTION_STATE")


def _validate_new_work_item(work_item: Mapping[str, Any]) -> None:
    if (
        work_item.get("status") != "TODO"
        or work_item.get("version") != 1
        or work_item.get("blocker") is not None
    ):
        raise ContinuityValidationError("INVALID_NEW_WORK_ITEM_STATE")


def _open_question_target(
    connection: sqlite3.Connection, question_id: str
) -> dict[str, Any]:
    row = _fetch_one(
        connection,
        "SELECT status, version, document FROM open_questions WHERE question_id = ?",
        (question_id,),
    )
    if row is None or row["status"] != "OPEN":
        raise ContinuityConflict("QUESTION_STATUS_MISMATCH")
    return row


def _ensure_available(
    connection: sqlite3.Connection, table: str, id_column: str, identifier: str
) -> None:
    if _fetch_one(
        connection,
        f"SELECT 1 AS present FROM {table} WHERE {id_column} = ?",
        (identifier,),
    ) is not None:
        raise ContinuityValidationError("DUPLICATE_OBJECT_ID")


def _fetch_one(
    connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...]
) -> dict[str, Any] | None:
    cursor = connection.execute(query, parameters)
    row = cursor.fetchone()
    if row is None:
        return None
    names = [description[0] for description in cursor.description]
    return dict(zip(names, tuple(row)))


def _fetch_all(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def _load_optional_json(value: str | None) -> Any:
    return None if value is None else json.loads(value)
