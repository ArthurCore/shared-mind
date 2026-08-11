"""Deterministic structured queries over the public projection surface."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import canonical_json
from .projection import project_json


QUERY_VERSION = "structured-query@1"

QUERY_KINDS = (
    "SOURCE_REVISION",
    "CLAIM",
    "EVIDENCE_LINK",
    "CONFLICT",
    "DECISION_RECORD",
    "OPEN_QUESTION",
    "WORK_ITEM",
)

_RECORD_ID = re.compile(
    r"^[a-z][a-z0-9_]{1,31}_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$"
)
_PREDICATE_KEY = re.compile(r"^[a-z][a-z0-9_.]*@[1-9][0-9]*$")
_SEMANTIC_ID = re.compile(
    r"^[a-z][a-z0-9_-]{1,31}:[a-z0-9][a-z0-9._-]{0,127}$"
)
_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

_QUERY_FIELDS = frozenset(
    {
        "kinds",
        "ids",
        "title_contains",
        "predicates",
        "source_ids",
        "source_revision_ids",
        "statuses",
        "limit",
        "offset",
        "include_record",
    }
)
_MAPPING_FIELDS = _QUERY_FIELDS | {"query_version"}


@dataclass(frozen=True)
class QuerySpec:
    """Strict, versioned-query inputs with AND-between/OR-within semantics."""

    kinds: tuple[str, ...] = ()
    ids: tuple[str, ...] = ()
    title_contains: str | None = None
    predicates: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_revision_ids: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    limit: int = 100
    offset: int = 0
    include_record: bool = True

    def __post_init__(self) -> None:
        for name in (
            "kinds",
            "ids",
            "predicates",
            "source_ids",
            "source_revision_ids",
            "statuses",
        ):
            object.__setattr__(self, name, _string_values(name, getattr(self, name)))
        if any(kind not in QUERY_KINDS for kind in self.kinds):
            raise ValueError("kinds contains an unsupported object kind")
        _require_pattern("ids", self.ids, _RECORD_ID)
        _require_pattern("predicates", self.predicates, _PREDICATE_KEY)
        _require_pattern("source_ids", self.source_ids, _SEMANTIC_ID)
        _require_pattern(
            "source_revision_ids", self.source_revision_ids, _RECORD_ID
        )
        _require_pattern("statuses", self.statuses, _STATUS)
        if self.title_contains is not None and (
            not isinstance(self.title_contains, str) or not self.title_contains
        ):
            raise ValueError("title_contains must be a non-empty string")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= self.limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if isinstance(self.offset, bool) or not isinstance(self.offset, int):
            raise ValueError("offset must be an integer")
        if self.offset < 0:
            raise ValueError("offset must be greater than or equal to zero")
        if not isinstance(self.include_record, bool):
            raise ValueError("include_record must be a boolean")

    def normalized(self) -> dict[str, Any]:
        return {
            "query_version": QUERY_VERSION,
            "kinds": list(self.kinds),
            "ids": list(self.ids),
            "title_contains": self.title_contains,
            "predicates": list(self.predicates),
            "source_ids": list(self.source_ids),
            "source_revision_ids": list(self.source_revision_ids),
            "statuses": list(self.statuses),
            "limit": self.limit,
            "offset": self.offset,
            "include_record": self.include_record,
        }


@dataclass(frozen=True)
class QueryResult:
    query_version: str
    projection_version: str
    state_root: str
    ledger_sequence: int
    normalized_query: dict[str, Any]
    hits: tuple[dict[str, Any], ...]
    total_matches: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_version": self.query_version,
            "projection_version": self.projection_version,
            "state_root": self.state_root,
            "ledger_sequence": self.ledger_sequence,
            "normalized_query": self.normalized_query,
            "hits": list(self.hits),
            "total_matches": self.total_matches,
            "truncated": self.truncated,
        }


def query(source: Any, spec: QuerySpec | Mapping[str, Any]) -> QueryResult:
    """Query one consistent, non-authoritative projection snapshot."""

    normalized_spec = _query_spec(spec)
    projection = json.loads(project_json(source))
    candidates = _candidates(projection)
    matching = [
        candidate
        for candidate in candidates
        if _matches(candidate, normalized_spec)
    ]
    kind_order = {kind: index for index, kind in enumerate(QUERY_KINDS)}
    matching.sort(
        key=lambda item: (kind_order[item["object_type"]], item["object_id"])
    )
    total_matches = len(matching)
    selected = matching[
        normalized_spec.offset : normalized_spec.offset + normalized_spec.limit
    ]
    hits = tuple(
        {
            "object_type": candidate["object_type"],
            "object_id": candidate["object_id"],
            "projection_ref": candidate["projection_ref"],
            "matched_fields": _matched_fields(candidate, normalized_spec),
            "summary": canonical_json(candidate["summary"]),
            "record": candidate["record"] if normalized_spec.include_record else None,
        }
        for candidate in selected
    )
    return QueryResult(
        query_version=QUERY_VERSION,
        projection_version=str(projection["projection_version"]),
        state_root=str(projection["state_root"]),
        ledger_sequence=int(projection["ledger"]["head_sequence"]),
        normalized_query=normalized_spec.normalized(),
        hits=hits,
        total_matches=total_matches,
        truncated=normalized_spec.offset + len(selected) < total_matches,
    )


def _query_spec(spec: QuerySpec | Mapping[str, Any]) -> QuerySpec:
    if isinstance(spec, QuerySpec):
        return spec
    if not isinstance(spec, Mapping):
        raise ValueError("query spec must be a QuerySpec or mapping")
    unknown = sorted(str(key) for key in set(spec) - _MAPPING_FIELDS)
    if unknown:
        raise ValueError("unknown query field(s): " + ", ".join(unknown))
    values = dict(spec)
    version = values.pop("query_version", QUERY_VERSION)
    if version != QUERY_VERSION:
        raise ValueError(f"unsupported query version: {version!r}")
    return QuerySpec(**values)


def _string_values(name: str, values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be an array of strings")
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{name} must contain only non-empty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")
    return tuple(sorted(values))


def _require_pattern(
    name: str, values: tuple[str, ...], pattern: re.Pattern[str]
) -> None:
    if any(pattern.fullmatch(value) is None for value in values):
        raise ValueError(f"{name} contains an invalid value")


def _candidates(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    source_id_by_revision = {
        record["source_revision"]["revision_id"]: record["source_revision"][
            "source_id"
        ]
        for record in projection["sources"]
    }
    evidence_revisions_by_claim: dict[str, set[str]] = {}
    for record in projection["evidence"]:
        evidence_revisions_by_claim.setdefault(record["claim_id"], set()).add(
            record["source_revision_id"]
        )
    claim_by_id = {
        record["claim"]["claim_id"]: record for record in projection["claims"]
    }

    candidates = [
        _source_candidate(record)
        for record in projection["sources"]
    ]
    candidates.extend(
        _claim_candidate(
            record,
            evidence_revisions_by_claim.get(record["claim"]["claim_id"], set()),
            source_id_by_revision,
        )
        for record in projection["claims"]
    )
    candidates.extend(
        _evidence_candidate(record, source_id_by_revision)
        for record in projection["evidence"]
    )
    candidates.extend(
        _conflict_candidate(
            record,
            claim_by_id,
            evidence_revisions_by_claim,
            source_id_by_revision,
        )
        for record in projection["conflicts"]
    )
    continuity_kinds = (
        ("DECISION_RECORD", "decisions", "decision_id"),
        ("OPEN_QUESTION", "questions", "question_id"),
        ("WORK_ITEM", "work_items", "work_item_id"),
    )
    for kind, section, id_field in continuity_kinds:
        candidates.extend(
            _continuity_candidate(
                kind,
                id_field,
                record,
                claim_by_id,
                evidence_revisions_by_claim,
                source_id_by_revision,
            )
            for record in projection["continuity"][section]
        )
    return candidates


def _source_candidate(record: Mapping[str, Any]) -> dict[str, Any]:
    document = record["source_revision"]
    revision_id = document["revision_id"]
    return _candidate(
        "SOURCE_REVISION",
        revision_id,
        record,
        title=document.get("title"),
        source_ids={document["source_id"]},
        source_revision_ids={revision_id},
        summary={
            "source_id": document["source_id"],
            "revision_id": revision_id,
            "title": document.get("title"),
            "media_type": document.get("media_type"),
            "content_hash": document.get("content_hash"),
        },
    )


def _claim_candidate(
    record: Mapping[str, Any],
    source_revision_ids: set[str],
    source_id_by_revision: Mapping[str, str],
) -> dict[str, Any]:
    document = record["claim"]
    proposition = record["proposition"]
    return _candidate(
        "CLAIM",
        document["claim_id"],
        record,
        predicates={proposition["predicate"]},
        source_ids=_source_ids(source_revision_ids, source_id_by_revision),
        source_revision_ids=source_revision_ids,
        status=record["status"],
        summary={
            "claim_id": document["claim_id"],
            "predicate": proposition["predicate"],
            "status": record["status"],
            "version": record["version"],
            "source_revision_count": len(source_revision_ids),
            "conflict_count": len(record["conflict_ids"]),
        },
    )


def _evidence_candidate(
    record: Mapping[str, Any], source_id_by_revision: Mapping[str, str]
) -> dict[str, Any]:
    document = record["evidence_link"]
    revision_ids = {record["source_revision_id"]}
    return _candidate(
        "EVIDENCE_LINK",
        record["evidence_link_id"],
        record,
        source_ids=_source_ids(revision_ids, source_id_by_revision),
        source_revision_ids=revision_ids,
        summary={
            "evidence_link_id": record["evidence_link_id"],
            "claim_id": record["claim_id"],
            "source_revision_id": record["source_revision_id"],
            "stance": document.get("stance"),
        },
    )


def _conflict_candidate(
    record: Mapping[str, Any],
    claim_by_id: Mapping[str, Mapping[str, Any]],
    evidence_revisions_by_claim: Mapping[str, set[str]],
    source_id_by_revision: Mapping[str, str],
) -> dict[str, Any]:
    members = set(record["members"])
    revisions = {
        revision_id
        for claim_id in members
        for revision_id in evidence_revisions_by_claim.get(claim_id, set())
    }
    predicates = {
        claim_by_id[claim_id]["proposition"]["predicate"]
        for claim_id in members
        if claim_id in claim_by_id
    }
    return _candidate(
        "CONFLICT",
        record["conflict_id"],
        record,
        predicates=predicates,
        source_ids=_source_ids(revisions, source_id_by_revision),
        source_revision_ids=revisions,
        status=record["status"],
        summary={
            "conflict_id": record["conflict_id"],
            "kind": record["kind"],
            "status": record["status"],
            "member_count": len(members),
            "member_digest": record["member_digest"],
        },
    )


def _continuity_candidate(
    kind: str,
    id_field: str,
    record: Mapping[str, Any],
    claim_by_id: Mapping[str, Mapping[str, Any]],
    evidence_revisions_by_claim: Mapping[str, set[str]],
    source_id_by_revision: Mapping[str, str],
) -> dict[str, Any]:
    document = record["document"]
    if not isinstance(document, Mapping):
        raise ValueError(f"{kind} projection record has no structured document")
    object_id = document[id_field]
    related_claim_ids = set(document.get("related_claim_ids", ()))
    related_revision_ids = set(document.get("related_source_revision_ids", ()))
    for reference in document.get("related_objects", ()):
        if reference.get("record_type") == "CLAIM":
            related_claim_ids.add(reference["record_id"])
        elif reference.get("record_type") == "SOURCE_REVISION":
            related_revision_ids.add(reference["record_id"])
    related_revision_ids.update(
        revision_id
        for claim_id in related_claim_ids
        for revision_id in evidence_revisions_by_claim.get(claim_id, set())
    )
    predicates = {
        claim_by_id[claim_id]["proposition"]["predicate"]
        for claim_id in related_claim_ids
        if claim_id in claim_by_id
    }
    title = document.get("title")
    return _candidate(
        kind,
        object_id,
        record,
        title=title,
        predicates=predicates,
        source_ids=_source_ids(related_revision_ids, source_id_by_revision),
        source_revision_ids=related_revision_ids,
        status=document.get("status"),
        summary={
            id_field: object_id,
            "title": title,
            "status": document.get("status"),
            "version": document.get("version"),
        },
    )


def _candidate(
    object_type: str,
    object_id: str,
    record: Mapping[str, Any],
    *,
    title: Any = None,
    predicates: set[str] | None = None,
    source_ids: set[str] | None = None,
    source_revision_ids: set[str] | None = None,
    status: Any = None,
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(object_id, str) or not object_id:
        raise ValueError(f"{object_type} projection record has no stable object id")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"{object_type} projection title must be a string")
    if status is not None and not isinstance(status, str):
        raise ValueError(f"{object_type} projection status must be a string")
    return {
        "object_type": object_type,
        "object_id": object_id,
        "projection_ref": record["projection_ref"],
        "title": title,
        "predicates": predicates or set(),
        "source_ids": source_ids or set(),
        "source_revision_ids": source_revision_ids or set(),
        "status": status,
        "summary": dict(summary),
        "record": dict(record),
    }


def _source_ids(
    revision_ids: set[str], source_id_by_revision: Mapping[str, str]
) -> set[str]:
    return {
        source_id_by_revision[revision_id]
        for revision_id in revision_ids
        if revision_id in source_id_by_revision
    }


def _matches(candidate: Mapping[str, Any], spec: QuerySpec) -> bool:
    return all(
        (
            not spec.kinds or candidate["object_type"] in spec.kinds,
            not spec.ids or candidate["object_id"] in spec.ids,
            spec.title_contains is None
            or (
                candidate["title"] is not None
                and spec.title_contains in candidate["title"]
            ),
            not spec.predicates
            or bool(candidate["predicates"].intersection(spec.predicates)),
            not spec.source_ids
            or bool(candidate["source_ids"].intersection(spec.source_ids)),
            not spec.source_revision_ids
            or bool(
                candidate["source_revision_ids"].intersection(
                    spec.source_revision_ids
                )
            ),
            not spec.statuses or candidate["status"] in spec.statuses,
        )
    )


def _matched_fields(candidate: Mapping[str, Any], spec: QuerySpec) -> list[str]:
    fields = []
    for field, active in (
        ("id", bool(spec.ids)),
        ("title", spec.title_contains is not None),
        ("predicate", bool(spec.predicates)),
        ("source_id", bool(spec.source_ids)),
        ("source_revision_id", bool(spec.source_revision_ids)),
        ("status", bool(spec.statuses)),
    ):
        if active:
            fields.append(field)
    return fields


__all__ = [
    "QUERY_KINDS",
    "QUERY_VERSION",
    "QueryResult",
    "QuerySpec",
    "query",
]
