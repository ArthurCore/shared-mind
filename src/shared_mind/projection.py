"""Deterministic, non-authoritative views over Shared Mind state.

The projector reads materialized SQLite state but never writes to it.  JSON is
the lossless machine-readable view; Markdown repeats canonical JSON records so
IDs, locators, lifecycle fields, and future continuity records remain
searchable without making Markdown authoritative.  Each projection owns a
read transaction for a consistent snapshot.  A caller-supplied connection
that already has an active transaction is rejected so uncommitted state can
never leak into a projection.
"""

from __future__ import annotations

import base64
import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .canonical import canonical_json, sha256_bytes, sha256_json


PROJECTION_VERSION = "markdown-projection@3"
CONTEXT_PACK_VERSION = "handoff-context@2"
DEFAULT_CONTEXT_BUDGET_BYTES = 32_000
TOKEN_BYTES_ESTIMATE = 4
TOKEN_ESTIMATOR_VERSION = "utf8-bytes-token-estimator@1"
CONTEXT_SELECTION_RULE_VERSION = "context-selection@2"
CONTEXT_SELECTION_RULE = (
    "mandatory-purpose,open-conflicts,active-decisions,open-questions,"
    "actionable-work-items;greedy:current_claims;stable-projection-order"
)


class ProjectionError(Exception):
    """Raised when a projection cannot be produced from the supplied source."""


class ContextBudgetError(ProjectionError):
    """Raised when mandatory context cannot fit without hiding open conflict data."""

    def __init__(self, required_bytes: int, budget_bytes: int) -> None:
        self.required_bytes = required_bytes
        self.budget_bytes = budget_bytes
        super().__init__(
            "context budget cannot expose mandatory purpose, continuity, and open conflicts: "
            f"requires {required_bytes} bytes, budget is {budget_bytes} bytes"
        )


def project_json(source: Any) -> str:
    """Return byte-stable JSON, rejecting caller-owned active transactions."""

    with _read_connection(source) as connection:
        return canonical_json(_build_projection(connection)) + "\n"


def project_markdown(source: Any) -> str:
    """Return lossless Markdown, rejecting caller-owned active transactions."""

    with _read_connection(source) as connection:
        projection = _build_projection(connection)
    return _render_markdown(projection)


def build_context_pack(
    source: Any,
    *,
    budget_bytes: int | None = None,
    budget_tokens: int | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic handoff object within the requested budget.

    Every open conflict and both/all of its member claims are mandatory.  A
    budget too small for that invariant raises :class:`ContextBudgetError`
    instead of returning a misleading partial view.  Token budgets use the
    declared, dependency-free ``ceil(utf8_bytes/4)`` estimate; callers that
    need model-exact accounting can pass the resulting byte limit from their
    tokenizer.
    """

    if purpose is not None and (not isinstance(purpose, str) or not purpose.strip()):
        raise ValueError("purpose must be a non-empty string when supplied")
    effective_budget = _effective_budget(budget_bytes, budget_tokens)
    with _read_connection(source) as connection:
        projection = _build_projection(connection)

    claim_by_id = {
        item["claim"]["claim_id"]: item for item in projection["claims"]
    }
    evidence_by_claim: dict[str, list[dict[str, Any]]] = {}
    for item in projection["evidence"]:
        evidence_by_claim.setdefault(item["claim_id"], []).append(item)

    open_conflicts = [
        _context_conflict(item, claim_by_id, evidence_by_claim)
        for item in projection["conflicts"]
        if item["status"] == "OPEN"
    ]
    conflicted_claim_ids = {
        member["claim_id"]
        for conflict in open_conflicts
        for member in conflict["members"]
    }
    current_claims = [
        _context_claim(item, evidence_by_claim.get(item["claim"]["claim_id"], []))
        for item in projection["claims"]
        if item["status"] == "ACTIVE"
        and item["claim"]["claim_id"] not in conflicted_claim_ids
    ]
    decisions = _active_continuity(
        projection["continuity"]["decisions"],
        active_statuses={"ACTIVE"},
        known_statuses={"ACTIVE", "SUPERSEDED", "REVERSED"},
        record_kind="decision",
    )
    questions = _active_continuity(
        projection["continuity"]["questions"],
        active_statuses={"OPEN"},
        known_statuses={"OPEN", "ANSWERED", "DROPPED"},
        record_kind="question",
    )
    work_items = _active_continuity(
        projection["continuity"]["work_items"],
        active_statuses={"TODO", "DOING", "BLOCKED"},
        known_statuses={"TODO", "DOING", "BLOCKED", "DONE", "DROPPED"},
        record_kind="work item",
    )

    sections: tuple[tuple[str, list[dict[str, Any]], str], ...] = (
        ("current_claims", current_claims, "project.json#/claims"),
    )
    pack: dict[str, Any] = {
        "context_pack_version": CONTEXT_PACK_VERSION,
        "projection_version": projection["projection_version"],
        "ledger_seq": projection["ledger"]["head_sequence"],
        "state_root": projection["state_root"],
        "purpose": purpose,
        "purpose_missing": purpose is None,
        "open_conflicts": open_conflicts,
        "decisions": decisions,
        "open_questions": questions,
        "work_items": work_items,
        "current_claims": [],
        "truncation": _truncation_metadata(
            effective_budget,
            budget_bytes,
            budget_tokens,
            included={name: 0 for name, _, _ in sections},
            omitted={name: len(items) for name, items, _ in sections},
            references=[],
            rendered_bytes=0,
        ),
    }

    minimum = _finalize_size(pack)
    if minimum > effective_budget:
        raise ContextBudgetError(minimum, effective_budget)

    included = {name: 0 for name, _, _ in sections}
    for name, items, _ in sections:
        for item in items:
            pack[name].append(item)
            included[name] += 1
            _refresh_truncation(pack, sections, included, effective_budget)
            if _finalize_size(pack) > effective_budget:
                pack[name].pop()
                included[name] -= 1
                _refresh_truncation(pack, sections, included, effective_budget)
                break

    _refresh_truncation(pack, sections, included, effective_budget)
    rendered_bytes = _finalize_size(pack)
    if rendered_bytes > effective_budget:
        # References and digit growth can make the final metadata larger than
        # the optimistic item check. Remove lowest-priority tail items until it
        # fits, while never touching mandatory open conflicts.
        for name, _, _ in reversed(sections):
            while pack[name] and rendered_bytes > effective_budget:
                pack[name].pop()
                included[name] -= 1
                _refresh_truncation(pack, sections, included, effective_budget)
                rendered_bytes = _finalize_size(pack)
        if rendered_bytes > effective_budget:
            raise ContextBudgetError(rendered_bytes, effective_budget)
    _set_stable_rendered_size(pack)
    return pack


def _build_projection(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = _table_names(connection)
    required = {"sources", "claims", "evidence", "conflicts", "ledger"}
    missing = sorted(required - tables)
    if missing:
        raise ProjectionError(f"missing required tables: {', '.join(missing)}")
    continuity_tables = {"decision_records", "open_questions", "work_items"}
    present_continuity_tables = continuity_tables & tables
    if present_continuity_tables and present_continuity_tables != continuity_tables:
        missing_continuity = sorted(continuity_tables - present_continuity_tables)
        raise ProjectionError(
            "incomplete continuity state: missing " + ", ".join(missing_continuity)
        )

    source_rows = _fetch_rows(connection, "sources")
    claim_rows = _fetch_rows(connection, "claims")
    evidence_rows = _fetch_rows(connection, "evidence")
    conflict_rows = _fetch_rows(connection, "conflicts")
    ledger_rows = _fetch_rows(connection, "ledger")

    history_by_id: dict[str, set[int]] = {}
    ledger_entries: list[dict[str, Any]] = []
    for index, row in enumerate(ledger_rows):
        proposal = _load_json(row.get("proposal"), {})
        events = _load_json(row.get("events"), [])
        ledger_document = _load_json(row.get("document"), None)
        schema_version = (
            proposal.get("versions", {}).get("schema")
            if isinstance(proposal, Mapping)
            else None
        )
        if schema_version not in {"1.0.0", "1.1.0"} and not isinstance(
            ledger_document, Mapping
        ):
            raise ProjectionError(
                f"ledger sequence {row['seq']} has no canonical LedgerEntry document"
            )
        sequence = int(row["seq"])
        for identifier in sorted(_find_object_ids(events) | _find_object_ids(proposal)):
            history_by_id.setdefault(identifier, set()).add(sequence)
        ledger_entries.append(
            {
                "sequence": sequence,
                "previous_hash": row.get("prev_hash"),
                "entry_hash": row["entry_hash"],
                "proposal_hash": row["proposal_hash"],
                "proposal": proposal,
                "events": events,
                "ledger_entry": ledger_document,
                "legacy_contract_incomplete": schema_version in {"1.0.0", "1.1.0"}
                and ledger_document is None,
                "pre_state_root": row.get("pre_state_root"),
                "state_root": row["state_root"],
                "committed_at": row.get("committed_at"),
                "projection_ref": f"project.json#/ledger/entries/{index}",
            }
        )
    history_ref_by_sequence = {
        entry["sequence"]: entry["projection_ref"] for entry in ledger_entries
    }

    sources = []
    for index, row in enumerate(source_rows):
        source_revision = _load_json(row.get("document"), {})
        content = bytes(row.get("content") or b"")
        revision_id = str(row["revision_id"])
        sources.append(
            {
                "source_revision": source_revision,
                "content_size_bytes": len(content),
                "verified_content_hash": sha256_bytes(content),
                "history_sequences": sorted(history_by_id.get(revision_id, set())),
                "history_refs": _history_refs(
                    revision_id, history_by_id, history_ref_by_sequence
                ),
                "projection_ref": f"project.json#/sources/{index}",
            }
        )

    evidence = []
    for index, row in enumerate(evidence_rows):
        link = _load_json(row.get("document"), {})
        link_id = str(row["evidence_link_id"])
        evidence.append(
            {
                "evidence_link": link,
                "evidence_link_id": link_id,
                "claim_id": str(row["claim_id"]),
                "source_revision_id": str(row["source_revision_id"]),
                "history_sequences": sorted(history_by_id.get(link_id, set())),
                "history_refs": _history_refs(
                    link_id, history_by_id, history_ref_by_sequence
                ),
                "projection_ref": f"project.json#/evidence/{index}",
            }
        )

    conflict_members_by_claim: dict[str, list[str]] = {}
    conflicts = []
    for index, row in enumerate(conflict_rows):
        conflict_id = str(row["conflict_id"])
        members = sorted(str(item) for item in _load_json(row.get("members"), []))
        for member in members:
            conflict_members_by_claim.setdefault(member, []).append(conflict_id)
        conflicts.append(
            {
                "conflict_id": conflict_id,
                "family_key": row["family_key"],
                "kind": row["kind"],
                "member_digest": row["member_digest"],
                "members": members,
                "status": row["status"],
                "episode": row["episode"],
                "version": row.get("version"),
                "resolution": _load_json(row.get("resolution"), None),
                "opened_sequence": row.get("opened_seq"),
                "history_sequences": sorted(history_by_id.get(conflict_id, set())),
                "history_refs": _history_refs(
                    conflict_id, history_by_id, history_ref_by_sequence
                ),
                "projection_ref": f"project.json#/conflicts/{index}",
            }
        )

    evidence_ids_by_claim: dict[str, list[str]] = {}
    for item in evidence:
        evidence_ids_by_claim.setdefault(item["claim_id"], []).append(
            item["evidence_link_id"]
        )
    claims = []
    for index, row in enumerate(claim_rows):
        claim_id = str(row["claim_id"])
        claims.append(
            {
                "claim": _load_json(row.get("document"), {}),
                "proposition": _load_json(row.get("proposition"), {}),
                "proposition_hash": row["proposition_hash"],
                "status": row["status"],
                "version": row["version"],
                "superseded_by": row.get("superseded_by"),
                "evidence_link_ids": sorted(evidence_ids_by_claim.get(claim_id, [])),
                "conflict_ids": sorted(conflict_members_by_claim.get(claim_id, [])),
                "history_sequences": sorted(history_by_id.get(claim_id, set())),
                "history_refs": _history_refs(
                    claim_id, history_by_id, history_ref_by_sequence
                ),
                "projection_ref": f"project.json#/claims/{index}",
            }
        )

    claim_ids = {item["claim"]["claim_id"] for item in claims}
    for conflict in conflicts:
        if conflict["status"] != "OPEN":
            continue
        missing_members = sorted(set(conflict["members"]) - claim_ids)
        if missing_members:
            raise ProjectionError(
                f"open conflict {conflict['conflict_id']} has missing member claim(s): "
                + ", ".join(missing_members)
            )

    continuity = {
        "decisions": _optional_records(
            connection,
            tables,
            ("decision_records", "decisions"),
            history_by_id,
            history_ref_by_sequence,
        ),
        "questions": _optional_records(
            connection,
            tables,
            ("open_questions", "questions"),
            history_by_id,
            history_ref_by_sequence,
        ),
        "work_items": _optional_records(
            connection,
            tables,
            ("work_items",),
            history_by_id,
            history_ref_by_sequence,
        ),
    }
    for section, records in continuity.items():
        for index, record in enumerate(records):
            record["projection_ref"] = (
                f"project.json#/continuity/{section}/{index}"
            )
    _validate_continuity_statuses(continuity)

    head = ledger_entries[-1] if ledger_entries else None
    return {
        "projection_version": PROJECTION_VERSION,
        "state_root": _state_root(connection, tables),
        "ledger": {
            "head_sequence": head["sequence"] if head else 0,
            "head_entry_hash": head["entry_hash"] if head else None,
            "entries": ledger_entries,
        },
        "sources": sources,
        "claims": claims,
        "evidence": evidence,
        "conflicts": conflicts,
        "continuity": continuity,
    }


def _render_markdown(projection: Mapping[str, Any]) -> str:
    ledger = projection["ledger"]
    lines = [
        "# Shared Mind Projection",
        "",
        f"- Projection version: `{projection['projection_version']}`",
        f"- Ledger head: `{ledger['head_sequence']}`",
        f"- State root: `{projection['state_root']}`",
        "",
    ]
    _markdown_records(lines, "Sources", projection["sources"], "source_revision")
    _markdown_records(lines, "Claims", projection["claims"], "claim")
    _markdown_records(lines, "Evidence", projection["evidence"], "evidence_link")
    _markdown_records(lines, "Conflicts", projection["conflicts"], "conflict_id")
    continuity = projection["continuity"]
    _markdown_records(lines, "Decisions", continuity["decisions"], None)
    _markdown_records(lines, "Questions", continuity["questions"], None)
    _markdown_records(lines, "Work Items", continuity["work_items"], None)
    _markdown_records(lines, "History", ledger["entries"], "sequence")
    return "\n".join(lines).rstrip() + "\n"


def _markdown_records(
    lines: list[str], title: str, records: list[dict[str, Any]], key: str | None
) -> None:
    lines.extend((f"## {title}", ""))
    if not records:
        lines.extend(("_None._", ""))
        return
    for record in records:
        identifier = _record_identifier(record, key)
        lines.extend((f"### {identifier}", ""))
        history = record.get("history_sequences")
        if history:
            joined = ", ".join(str(item) for item in history)
            lines.extend((f"History: ledger sequence {joined}", ""))
        lines.extend(("```json", canonical_json(record), "```", ""))


def _record_identifier(record: Mapping[str, Any], key: str | None) -> str:
    if key and key in record:
        value = record[key]
        if isinstance(value, Mapping):
            for candidate in (
                "revision_id",
                "claim_id",
                "evidence_link_id",
                "id",
            ):
                if candidate in value:
                    return str(value[candidate])
        return str(value)
    row = record.get("row", {})
    if isinstance(row, Mapping) and row:
        return str(next(iter(row.values())))
    return str(record.get("table", "record"))


def _context_claim(
    claim: Mapping[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    claim_id = claim["claim"]["claim_id"]
    return {
        "claim_id": claim_id,
        "status": claim["status"],
        "version": claim["version"],
        "proposition_hash": claim["proposition_hash"],
        "proposition": claim["proposition"],
        "evidence": [_context_evidence(item) for item in evidence],
        "projection_ref": claim["projection_ref"],
        "history_sequences": claim["history_sequences"],
        "history_refs": claim["history_refs"],
    }


def _context_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    link = evidence["evidence_link"]
    selector = link.get("selector", {})
    return {
        "evidence_link_id": evidence["evidence_link_id"],
        "source_revision_id": evidence["source_revision_id"],
        "stance": link.get("stance"),
        "selector": {
            name: selector.get(name)
            for name in (
                "start_byte",
                "end_byte",
                "start_line",
                "end_line",
                "excerpt_hash",
            )
            if name in selector
        },
        "projection_ref": evidence["projection_ref"],
    }


def _context_conflict(
    conflict: Mapping[str, Any],
    claim_by_id: Mapping[str, Mapping[str, Any]],
    evidence_by_claim: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    members = []
    for claim_id in conflict["members"]:
        claim = claim_by_id.get(claim_id)
        if claim is None:
            raise ProjectionError(
                f"open conflict {conflict['conflict_id']} has missing member claim: "
                f"{claim_id}"
            )
        else:
            members.append(_context_claim(claim, evidence_by_claim.get(claim_id, [])))
    return {
        "conflict_id": conflict["conflict_id"],
        "kind": conflict["kind"],
        "status": conflict["status"],
        "episode": conflict["episode"],
        "member_digest": conflict["member_digest"],
        "members": members,
        "projection_ref": conflict["projection_ref"],
        "history_sequences": conflict["history_sequences"],
        "history_refs": conflict["history_refs"],
    }


def _active_continuity(
    records: list[dict[str, Any]],
    *,
    active_statuses: set[str],
    known_statuses: set[str],
    record_kind: str,
) -> list[dict[str, Any]]:
    result = []
    for record in records:
        status = _find_status(record["row"])
        if status not in known_statuses:
            rendered = "<missing>" if status is None else status
            raise ProjectionError(f"unknown {record_kind} status: {rendered}")
        if status in active_statuses:
            result.append(record)
    return result


def _validate_continuity_statuses(
    continuity: Mapping[str, list[dict[str, Any]]],
) -> None:
    specifications = (
        ("decisions", "decision", {"ACTIVE", "SUPERSEDED", "REVERSED"}),
        ("questions", "question", {"OPEN", "ANSWERED", "DROPPED"}),
        (
            "work_items",
            "work item",
            {"TODO", "DOING", "BLOCKED", "DONE", "DROPPED"},
        ),
    )
    for section, record_kind, known_statuses in specifications:
        for record in continuity[section]:
            status = _find_status(record["row"])
            if status not in known_statuses:
                rendered = "<missing>" if status is None else status
                raise ProjectionError(f"unknown {record_kind} status: {rendered}")


def _find_status(row: Mapping[str, Any]) -> str | None:
    direct = row.get("status")
    if isinstance(direct, str):
        return direct
    for name in ("document", "payload"):
        parsed = _load_json(row.get(name), None)
        if isinstance(parsed, Mapping) and isinstance(parsed.get("status"), str):
            return parsed["status"]
    return None


def _effective_budget(
    budget_bytes: int | None, budget_tokens: int | None
) -> int:
    for name, value in (("budget_bytes", budget_bytes), ("budget_tokens", budget_tokens)):
        if value is not None and (isinstance(value, bool) or value <= 0):
            raise ValueError(f"{name} must be a positive integer")
    limits = []
    if budget_bytes is not None:
        limits.append(int(budget_bytes))
    if budget_tokens is not None:
        limits.append(int(budget_tokens) * TOKEN_BYTES_ESTIMATE)
    return min(limits) if limits else DEFAULT_CONTEXT_BUDGET_BYTES


def _truncation_metadata(
    effective_budget: int,
    requested_bytes: int | None,
    requested_tokens: int | None,
    *,
    included: Mapping[str, int],
    omitted: Mapping[str, int],
    references: list[dict[str, Any]],
    rendered_bytes: int,
) -> dict[str, Any]:
    return {
        "truncated": any(omitted.values()),
        "budget_bytes": effective_budget,
        "requested_budget_bytes": requested_bytes,
        "requested_budget_tokens": requested_tokens,
        "rendered_bytes": rendered_bytes,
        "estimated_tokens": math.ceil(rendered_bytes / TOKEN_BYTES_ESTIMATE),
        "token_estimator": "ceil(utf8_bytes/4)",
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "token_estimate_exact": False,
        "selection_rule": CONTEXT_SELECTION_RULE,
        "selection_rule_version": CONTEXT_SELECTION_RULE_VERSION,
        "included_counts": dict(included),
        "omitted_counts": dict(omitted),
        "references": references,
    }


def _refresh_truncation(
    pack: dict[str, Any],
    sections: tuple[tuple[str, list[dict[str, Any]], str], ...],
    included: Mapping[str, int],
    effective_budget: int,
) -> None:
    omitted = {name: len(items) - included[name] for name, items, _ in sections}
    references = [
        {
            "section": name,
            "omitted_count": omitted[name],
            "projection_ref": reference,
        }
        for name, _, reference in sections
        if omitted[name]
    ]
    prior = pack["truncation"]
    pack["truncation"] = _truncation_metadata(
        effective_budget,
        prior["requested_budget_bytes"],
        prior["requested_budget_tokens"],
        included=included,
        omitted=omitted,
        references=references,
        rendered_bytes=0,
    )
    _set_stable_rendered_size(pack)


def _set_stable_rendered_size(pack: dict[str, Any]) -> None:
    previous = -1
    for _ in range(8):
        size = _finalize_size(pack)
        if size == previous and pack["truncation"]["rendered_bytes"] == size:
            return
        pack["truncation"]["rendered_bytes"] = size
        pack["truncation"]["estimated_tokens"] = math.ceil(
            size / TOKEN_BYTES_ESTIMATE
        )
        previous = size
    raise ProjectionError("context size metadata did not converge")


def _finalize_size(pack: Mapping[str, Any]) -> int:
    return len(canonical_json(pack).encode("utf-8"))


def _optional_records(
    connection: sqlite3.Connection,
    tables: set[str],
    candidates: tuple[str, ...],
    history_by_id: Mapping[str, set[int]],
    history_ref_by_sequence: Mapping[int, str],
) -> list[dict[str, Any]]:
    records = []
    for table in candidates:
        if table not in tables:
            continue
        for row in _fetch_rows(connection, table):
            identifier = _row_identifier(row)
            records.append(
                {
                    "table": table,
                    "row": _json_safe(row),
                    "document": _load_json(row.get("document"), None),
                    "history_sequences": sorted(
                        history_by_id.get(identifier, set())
                    ),
                    "history_refs": _history_refs(
                        identifier, history_by_id, history_ref_by_sequence
                    ),
                }
            )
    return sorted(records, key=canonical_json)


def _row_identifier(row: Mapping[str, Any]) -> str:
    for key, value in row.items():
        if (key == "id" or key.endswith("_id")) and isinstance(value, str):
            return value
    raise ProjectionError("continuity row has no stable object identifier")


def _history_refs(
    identifier: str,
    history_by_id: Mapping[str, set[int]],
    history_ref_by_sequence: Mapping[int, str],
) -> list[str]:
    return [
        history_ref_by_sequence[sequence]
        for sequence in sorted(history_by_id.get(identifier, set()))
        if sequence in history_ref_by_sequence
    ]


def _state_root(connection: sqlite3.Connection, tables: set[str]) -> str:
    head = connection.execute(
        "SELECT proposal FROM ledger ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    head_proposal = _load_json(head[0], {}) if head is not None else {}
    schema_version = (
        head_proposal.get("versions", {}).get("schema")
        if isinstance(head_proposal, Mapping)
        else None
    )
    legacy = schema_version == "1.0.0"
    state: dict[str, list[Any]] = {}
    for table in ("sources", "claims", "evidence", "conflicts"):
        if table == "conflicts" and legacy:
            cursor = connection.execute(
                "SELECT conflict_id, family_key, kind, member_digest, members, "
                "status, episode FROM conflicts ORDER BY conflict_id"
            )
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, tuple(row))) for row in cursor.fetchall()]
        else:
            rows = _fetch_rows(connection, table)
        normalized = []
        for row in rows:
            item = dict(row)
            if table == "sources":
                item["content"] = sha256_bytes(bytes(item["content"]))
            normalized.append(item)
        state[table] = normalized
    continuity_tables = {"decision_records", "open_questions", "work_items"}
    if not legacy and continuity_tables.issubset(tables):
        from .continuity import state_rows as continuity_state_rows

        state.update(continuity_state_rows(connection))
    return sha256_json(state)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    }


def _fetch_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in _table_names(connection):
        return []
    quoted = table.replace('"', '""')
    cursor = connection.execute(f'SELECT * FROM "{quoted}" ORDER BY 1')
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"$binary_base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _load_json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _find_object_ids(value: Any) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "versions":
                continue
            if (key == "id" or key.endswith("_id")) and isinstance(item, str):
                identifiers.add(item)
            identifiers.update(_find_object_ids(item))
    elif isinstance(value, list):
        for item in value:
            identifiers.update(_find_object_ids(item))
    return identifiers


@contextmanager
def _read_connection(source: Any) -> Iterator[sqlite3.Connection]:
    connection = getattr(source, "connection", source)
    if isinstance(connection, sqlite3.Connection):
        if connection.in_transaction:
            raise ProjectionError(
                "cannot project from an active transaction; commit or roll back first"
            )
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        return
    if isinstance(connection, (str, Path)):
        path = Path(connection)
        if not path.is_file():
            raise ProjectionError(f"database does not exist: {path}")
        owned = sqlite3.connect(str(path))
        try:
            owned.execute("BEGIN")
            try:
                yield owned
            finally:
                if owned.in_transaction:
                    owned.execute("ROLLBACK")
        finally:
            owned.close()
        return
    raise TypeError("source must be a Kernel, sqlite3.Connection, or database path")


__all__ = [
    "CONTEXT_PACK_VERSION",
    "DEFAULT_CONTEXT_BUDGET_BYTES",
    "PROJECTION_VERSION",
    "ContextBudgetError",
    "ProjectionError",
    "build_context_pack",
    "project_json",
    "project_markdown",
]
