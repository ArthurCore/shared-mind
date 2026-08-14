"""Derived shared-memory views and deterministic task-aware context routing."""

from __future__ import annotations

import json
import re
import copy
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .canonical import canonical_json, sha256_json
from .product_contract import validate_product_object
from .product_store import ProductStore, utc_now
from .projection import ContextBudgetError, build_context_pack, project_json
from .skills import select_skills
from .workspace import Workspace


MEMORY_BUILDER_VERSION = "memory-views@1"
CONTEXT_SELECTOR_VERSION = "task-context-selector@1"
DEFAULT_CONTEXT_BUDGET_BYTES = 64 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_:.@/-]+")


class MemoryViewError(Exception):
    def __init__(self, code: str, message: str, *, data: Any | None = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


class MemoryViewBuilder:
    def __init__(self, workspace: Workspace, store: ProductStore):
        self.workspace = workspace
        self.store = store

    def projection(self) -> dict[str, Any]:
        kernel = self.workspace.open_kernel()
        try:
            return json.loads(project_json(kernel))
        finally:
            kernel.close()

    def atomic_records(self, projection: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        state = dict(projection or self.projection())
        records: list[dict[str, Any]] = []
        source_by_revision = {
            item["source_revision"]["revision_id"]: item for item in state["sources"]
        }
        evidence_by_claim: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in state["evidence"]:
            evidence_by_claim[item["claim_id"]].append(item)
        for item in state["sources"]:
            source = item["source_revision"]
            records.append(
                {
                    "object_id": source["revision_id"],
                    "kind": "SOURCE_REVISION",
                    "title": source["title"],
                    "summary": f"{source['media_type']} source {source['source_id']}",
                    "status": "IMMUTABLE",
                    "source_revision_ids": [source["revision_id"]],
                    "related_ids": [],
                    "projection_ref": item["projection_ref"],
                    "history_sequences": list(item.get("history_sequences", [])),
                    "history_refs": list(item.get("history_refs", [])),
                    "document": source,
                }
            )
        for item in state["claims"]:
            claim = item["claim"]
            proposition = item["proposition"]
            object_id = claim["claim_id"]
            object_text = _object_text(proposition.get("object", {}))
            subject = proposition.get("subject", {}).get("entity_id", "unknown")
            records.append(
                {
                    "object_id": object_id,
                    "kind": "CLAIM",
                    "title": f"{subject} {proposition.get('predicate')} {object_text}",
                    "summary": canonical_json(proposition),
                    "status": item["status"],
                    "source_revision_ids": sorted(
                        {
                            evidence["source_revision_id"]
                            for evidence in evidence_by_claim.get(object_id, [])
                        }
                    ),
                    "related_ids": sorted(
                        set(item.get("evidence_link_ids", []))
                        | set(item.get("conflict_ids", []))
                    ),
                    "projection_ref": item["projection_ref"],
                    "history_sequences": list(item.get("history_sequences", [])),
                    "history_refs": list(item.get("history_refs", [])),
                    "document": claim,
                    "evidence": [
                        evidence["evidence_link"]
                        for evidence in evidence_by_claim.get(object_id, [])
                    ],
                }
            )
        for item in state["conflicts"]:
            records.append(
                {
                    "object_id": item["conflict_id"],
                    "kind": "CONFLICT",
                    "title": f"{item['kind']} conflict",
                    "summary": f"Members: {', '.join(item['members'])}",
                    "status": item["status"],
                    "source_revision_ids": [],
                    "related_ids": list(item["members"]),
                    "projection_ref": item["projection_ref"],
                    "history_sequences": list(item.get("history_sequences", [])),
                    "history_refs": list(item.get("history_refs", [])),
                    "document": dict(item),
                }
            )
        continuity_map = (
            ("decisions", "DECISION_RECORD", "decision_id", "title", "conclusion"),
            ("questions", "OPEN_QUESTION", "question_id", "question", "context"),
            ("work_items", "WORK_ITEM", "work_item_id", "description", "description"),
        )
        for section, kind, id_field, title_field, summary_field in continuity_map:
            for item in state["continuity"][section]:
                document = item["document"]
                if not isinstance(document, Mapping):
                    continue
                related = _related_ids(document)
                source_ids = sorted(
                    set(document.get("related_source_revision_ids", []))
                    | {
                        ref["record_id"]
                        for ref in document.get("related_objects", [])
                        if ref.get("record_type") == "SOURCE_REVISION"
                    }
                )
                records.append(
                    {
                        "object_id": document[id_field],
                        "kind": kind,
                        "title": str(document[title_field]),
                        "summary": str(document[summary_field]),
                        "status": document["status"],
                        "source_revision_ids": source_ids,
                        "related_ids": related,
                        "projection_ref": item["projection_ref"],
                        "history_sequences": list(item.get("history_sequences", [])),
                        "history_refs": list(item.get("history_refs", [])),
                        "document": dict(document),
                    }
                )
        records.sort(key=lambda item: (item["kind"], item["object_id"]))
        return records

    def build_all(self) -> dict[str, Any]:
        projection = self.projection()
        atomic = self.atomic_records(projection)
        state_root = projection["state_root"]
        built: list[dict[str, Any]] = []
        built.append(self._build_atomic_artifact(atomic, state_root))
        scenarios = self._scenario_documents(atomic, projection)
        active_ids: set[str] = set()
        for scenario in scenarios:
            artifact = self._artifact(
                artifact_id=scenario["artifact_id"],
                artifact_type="SCENARIO",
                scope=scenario["scope"],
                title=scenario["title"],
                document=scenario["document"],
                state_root=state_root,
                member_ids=scenario["document"]["member_object_ids"],
                member_dependencies=scenario["member_dependencies"],
            )
            with self.store.transaction():
                stored = self.store.put_artifact(artifact)
            built.append(stored)
            active_ids.add(stored["artifact_id"])
        core = self._build_core_artifact(projection, state_root)
        built.append(core)
        active_ids.update(item["artifact_id"] for item in built)
        stale_candidates = [
            item["artifact_id"]
            for item in self.store.list_artifacts(status="READY")
            if item["artifact_type"] in {"ATOMIC_MAP", "SCENARIO", "CORE_CONTEXT"}
            and item["artifact_id"] not in active_ids
        ]
        with self.store.transaction():
            stale_count = self.store.mark_artifacts_stale(stale_candidates)
        return {
            "state_root": state_root,
            "artifacts": sorted(item["artifact_id"] for item in built),
            "built": len(built),
            "stale": stale_count,
        }

    def _build_atomic_artifact(
        self, records: Sequence[Mapping[str, Any]], state_root: str
    ) -> dict[str, Any]:
        document = {
            "view_version": "atomic-map@1",
            "state_root": state_root,
            "records": [dict(record) for record in records],
            "member_object_ids": [record["object_id"] for record in records],
        }
        artifact = self._artifact(
            artifact_id="artifact_atomic-project",
            artifact_type="ATOMIC_MAP",
            scope="project",
            title="Project atomic shared state",
            document=document,
            state_root=state_root,
            member_ids=document["member_object_ids"],
        )
        with self.store.transaction():
            return self.store.put_artifact(artifact)

    def _build_core_artifact(
        self, projection: Mapping[str, Any], state_root: str
    ) -> dict[str, Any]:
        kernel = self.workspace.open_kernel()
        try:
            pack = build_context_pack(
                kernel,
                budget_bytes=1024 * 1024,
                purpose=self.workspace.purpose,
            )
        finally:
            kernel.close()
        member_ids = sorted(
            item
            for item in (
                set(_find_ids(pack))
                | {
                    document.get("decision_id")
                    for document in pack.get("decisions", [])
                    if isinstance(document, Mapping)
                }
            )
            if isinstance(item, str)
        )
        document = {
            "view_version": "core-context@1",
            "state_root": state_root,
            "purpose": self.workspace.purpose,
            "context": pack,
            "member_object_ids": member_ids,
            "authoritative": False,
        }
        artifact = self._artifact(
            artifact_id="artifact_core-project",
            artifact_type="CORE_CONTEXT",
            scope="project",
            title="Project core context",
            document=document,
            state_root=state_root,
            member_ids=member_ids,
        )
        with self.store.transaction():
            return self.store.put_artifact(artifact)

    def _scenario_documents(
        self,
        records: Sequence[Mapping[str, Any]],
        projection: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        scenarios: list[dict[str, Any]] = []
        record_by_id = {str(record["object_id"]): record for record in records}

        def dependency_map(member_ids: Sequence[str]) -> dict[str, str]:
            return {
                member_id: sha256_json(record_by_id[member_id])
                for member_id in sorted(set(member_ids))
                if member_id in record_by_id
            }

        def add_scenario(
            *,
            artifact_id: str,
            scope: str,
            title: str,
            document: Mapping[str, Any],
        ) -> None:
            member_ids = sorted(set(str(item) for item in document["member_object_ids"]))
            normalized_document = dict(document)
            normalized_document["member_object_ids"] = member_ids
            scenarios.append(
                {
                    "artifact_id": artifact_id,
                    "scope": scope,
                    "title": title,
                    "document": normalized_document,
                    "member_dependencies": dependency_map(member_ids),
                }
            )

        active = [
            record
            for record in records
            if record["kind"] != "SOURCE_REVISION"
            and record["status"] not in {"SUPERSEDED", "REVERSED", "ANSWERED", "DROPPED", "DONE", "DEPRECATED"}
        ]
        add_scenario(
            artifact_id="artifact_scenario-project",
            scope="project",
            title="Current project state",
            document={
                "scenario_version": "scenario@1",
                "scenario_kind": "PROJECT",
                "summary": _scenario_summary(active),
                "member_object_ids": [record["object_id"] for record in active],
                "open_conflict_ids": [
                    record["object_id"]
                    for record in active
                    if record["kind"] == "CONFLICT" and record["status"] == "OPEN"
                ],
            },
        )
        by_subject: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for record in records:
            if record["kind"] != "CLAIM":
                continue
            subject = record["document"].get("proposition", {}).get("subject", {}).get("entity_id")
            if subject:
                by_subject[str(subject)].append(record)
        conflict_by_member: dict[str, list[str]] = defaultdict(list)
        for conflict in projection["conflicts"]:
            if conflict["status"] != "OPEN":
                continue
            for member in conflict["members"]:
                conflict_by_member[member].append(conflict["conflict_id"])
        for subject, subject_records in sorted(by_subject.items()):
            member_ids = [record["object_id"] for record in subject_records]
            conflict_ids = sorted(
                {
                    conflict_id
                    for member in member_ids
                    for conflict_id in conflict_by_member.get(member, [])
                }
            )
            member_ids.extend(conflict_ids)
            digest = sha256_json(subject).split(":", 1)[1][:16]
            add_scenario(
                artifact_id=f"artifact_scenario-subject-{digest}",
                scope=subject,
                title=f"Knowledge about {subject}",
                document={
                    "scenario_version": "scenario@1",
                    "scenario_kind": "SUBJECT",
                    "subject_id": subject,
                    "summary": _scenario_summary(subject_records),
                    "member_object_ids": member_ids,
                    "open_conflict_ids": conflict_ids,
                },
            )

        # A decision thread is a local view over one decision and the objects
        # it explicitly references.  The view never becomes a second source of
        # truth; it can be discarded and rebuilt from the canonical records.
        decisions = [record for record in records if record["kind"] == "DECISION_RECORD"]
        for decision in decisions:
            member_ids = {str(decision["object_id"]), *map(str, decision["related_ids"])}
            replacement = decision["document"].get("replaced_by_decision_id")
            if isinstance(replacement, str):
                member_ids.add(replacement)
            # Preserve the reverse edge so a replacement decision can show the
            # superseded record without relying on a global project scenario.
            for other in decisions:
                if other["document"].get("replaced_by_decision_id") == decision["object_id"]:
                    member_ids.add(str(other["object_id"]))
            member_records = [record_by_id[item] for item in sorted(member_ids) if item in record_by_id]
            conflict_ids = sorted(
                item["object_id"]
                for item in member_records
                if item["kind"] == "CONFLICT" and item["status"] == "OPEN"
            )
            digest = sha256_json(str(decision["object_id"])).split(":", 1)[1][:16]
            add_scenario(
                artifact_id=f"artifact_scenario-decision-{digest}",
                scope=f"decision:{decision['object_id']}",
                title=f"Decision thread: {decision['title']}",
                document={
                    "scenario_version": "scenario@1",
                    "scenario_kind": "DECISION_THREAD",
                    "decision_id": decision["object_id"],
                    "summary": _scenario_summary(member_records),
                    "member_object_ids": sorted(member_ids),
                    "open_conflict_ids": conflict_ids,
                },
            )

        # Open factual conflicts are incidents in the shared state.  Include
        # both claims and the immutable sources that support them so context
        # routing never presents only one side of the contradiction.
        for conflict in sorted(
            (record for record in records if record["kind"] == "CONFLICT" and record["status"] == "OPEN"),
            key=lambda item: str(item["object_id"]),
        ):
            member_ids = {str(conflict["object_id"]), *map(str, conflict["related_ids"])}
            for claim_id in conflict["related_ids"]:
                claim = record_by_id.get(str(claim_id))
                if claim:
                    member_ids.update(map(str, claim["source_revision_ids"]))
            member_records = [record_by_id[item] for item in sorted(member_ids) if item in record_by_id]
            digest = sha256_json(str(conflict["object_id"])).split(":", 1)[1][:16]
            add_scenario(
                artifact_id=f"artifact_scenario-incident-{digest}",
                scope=f"conflict:{conflict['object_id']}",
                title=f"Open conflict: {conflict['title']}",
                document={
                    "scenario_version": "scenario@1",
                    "scenario_kind": "INCIDENT",
                    "conflict_id": conflict["object_id"],
                    "summary": _scenario_summary(member_records),
                    "member_object_ids": sorted(member_ids),
                    "open_conflict_ids": [conflict["object_id"]],
                },
            )

        # Workstream views keep an active WorkItem and its direct dependency
        # records together.  Completed/dropped work remains available in the
        # atomic/history views but does not create an active workstream.
        for work_item in sorted(
            (
                record
                for record in records
                if record["kind"] == "WORK_ITEM"
                and record["status"] not in {"DONE", "DROPPED"}
            ),
            key=lambda item: str(item["object_id"]),
        ):
            member_ids = {str(work_item["object_id"]), *map(str, work_item["related_ids"])}
            member_records = [record_by_id[item] for item in sorted(member_ids) if item in record_by_id]
            conflict_ids = sorted(
                item["object_id"]
                for item in member_records
                if item["kind"] == "CONFLICT" and item["status"] == "OPEN"
            )
            digest = sha256_json(str(work_item["object_id"])).split(":", 1)[1][:16]
            add_scenario(
                artifact_id=f"artifact_scenario-work-{digest}",
                scope=f"work:{work_item['object_id']}",
                title=f"Workstream: {work_item['title']}",
                document={
                    "scenario_version": "scenario@1",
                    "scenario_kind": "WORKSTREAM",
                    "work_item_id": work_item["object_id"],
                    "summary": _scenario_summary(member_records),
                    "member_object_ids": sorted(member_ids),
                    "open_conflict_ids": conflict_ids,
                },
            )
        return scenarios

    def _artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        scope: str,
        title: str,
        document: Mapping[str, Any],
        state_root: str,
        member_ids: Sequence[str],
        member_dependencies: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        dependency_input: dict[str, Any] = {
            "builder_version": MEMORY_BUILDER_VERSION,
            "artifact_type": artifact_type,
            "scope": scope,
            "member_ids": sorted(member_ids),
            "member_dependencies": dict(sorted((member_dependencies or {}).items())),
            "document": dict(document),
        }
        # Project-wide artifacts intentionally track the whole state.  A
        # Scenario only tracks its local member records; including the global
        # root would rebuild every Scenario after any unrelated change.
        if artifact_type != "SCENARIO":
            dependency_input["state_root"] = state_root
        dependency_digest = sha256_json(dependency_input)
        existing = self.store.get_artifact(artifact_id)
        created_at = existing["created_at"] if existing else utc_now()
        updated_at = utc_now()
        artifact = {
            "object_type": "DERIVED_MEMORY_ARTIFACT",
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "scope": scope,
            "title": title,
            "status": "READY",
            "version": existing["version"] if existing else 1,
            "dependency_digest": dependency_digest,
            "builder_version": MEMORY_BUILDER_VERSION,
            "created_at": created_at,
            "updated_at": updated_at,
            "document": dict(document),
            "provenance": {
                "kernel_state_root": state_root,
                "builder_version": MEMORY_BUILDER_VERSION,
                "member_object_ids": sorted(member_ids),
                "member_dependency_digests": dict(
                    sorted((member_dependencies or {}).items())
                ),
            },
        }
        issues = validate_product_object(artifact, "DerivedMemoryArtifact")
        if issues:
            raise MemoryViewError("ARTIFACT_INVALID", canonical_json(issues))
        return artifact

    def drill_down(self, object_id: str) -> dict[str, Any]:
        projection = self.projection()
        atomic = {record["object_id"]: record for record in self.atomic_records(projection)}
        ledger_by_sequence = {
            int(item["sequence"]): item for item in projection["ledger"]["entries"]
        }
        kernel = self.workspace.open_kernel()
        try:
            receipts_by_sequence: dict[int, list[dict[str, Any]]] = defaultdict(list)
            receipts_by_proposal: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in kernel.connection.execute(
                "SELECT ledger_seq, proposal_id, document FROM receipts ORDER BY id"
            ):
                receipt = json.loads(row["document"])
                if row["ledger_seq"] is not None:
                    receipts_by_sequence[int(row["ledger_seq"])].append(receipt)
                if row["proposal_id"] is not None:
                    receipts_by_proposal[str(row["proposal_id"])].append(receipt)
        finally:
            kernel.close()

        def history_for(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
            sequences = sorted(
                {
                    int(sequence)
                    for record in records
                    for sequence in record.get("history_sequences", [])
                }
            )
            history: list[dict[str, Any]] = []
            for sequence in sequences:
                entry = ledger_by_sequence.get(sequence)
                if entry is None:
                    continue
                proposal = entry.get("proposal") or {}
                proposal_id = proposal.get("proposal_id") if isinstance(proposal, Mapping) else None
                receipts = list(receipts_by_sequence.get(sequence, []))
                if not receipts and isinstance(proposal_id, str):
                    receipts = list(receipts_by_proposal.get(proposal_id, []))
                history.append(
                    {
                        "sequence": sequence,
                        "projection_ref": entry.get("projection_ref"),
                        "proposal": proposal,
                        "ledger_entry": entry.get("ledger_entry"),
                        "receipts": receipts,
                    }
                )
            return history

        artifact = self.store.get_artifact(object_id)
        if artifact:
            members = artifact["document"].get("member_object_ids", [])
            member_records = [atomic[item] for item in members if item in atomic]
            return {
                "object": artifact,
                "members": member_records,
                "missing_member_ids": [item for item in members if item not in atomic],
                "history": history_for(member_records),
            }
        record = atomic.get(object_id)
        if record is None:
            raise MemoryViewError("OBJECT_NOT_FOUND", f"Object not found: {object_id}")
        sources = {
            item["source_revision"]["revision_id"]: item["source_revision"]
            for item in projection["sources"]
        }
        return {
            "object": record,
            "sources": [sources[item] for item in record["source_revision_ids"] if item in sources],
            "evidence": record.get("evidence", []),
            "related": [atomic[item] for item in record["related_ids"] if item in atomic],
            "history": history_for([record]),
        }


class ContextRouter:
    """Deterministic context selection over one shared state."""

    def __init__(self, workspace: Workspace, store: ProductStore):
        self.workspace = workspace
        self.store = store
        self.views = MemoryViewBuilder(workspace, store)

    def route(self, request: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_context_request(request)
        issues = validate_product_object(normalized, "ContextRequest")
        if issues:
            raise MemoryViewError("CONTEXT_REQUEST_INVALID", canonical_json(issues))
        budget = _effective_budget(normalized)
        projection = self.views.projection()
        atomic = self.views.atomic_records(projection)
        references = set(normalized["references"])
        query_text = " ".join(
            item
            for item in (normalized["task"], normalized.get("purpose"), normalized.get("query"))
            if item
        )
        terms = _terms(query_text)
        scored: list[tuple[int, str, dict[str, Any], list[str]]] = []
        for record in atomic:
            score, reasons = _record_score(record, terms, references)
            if score > 0 or record["object_id"] in references:
                scored.append((score, record["object_id"], record, reasons))
        scored.sort(key=lambda item: (-item[0], item[1]))
        scenarios = []
        for artifact in self.store.list_artifacts(artifact_type="SCENARIO", status="READY"):
            score, reasons = _artifact_score(artifact, terms, references)
            if score > 0 or artifact["artifact_id"] in references:
                scenarios.append((score, artifact["artifact_id"], artifact, reasons))
        scenarios.sort(key=lambda item: (-item[0], item[1]))
        skills = select_skills(self.store, query_text, limit=10)
        kernel = self.workspace.open_kernel()
        try:
            core_budget = max(256, min(budget, int(budget * 0.72)))
            core = build_context_pack(
                kernel,
                budget_bytes=core_budget,
                purpose=normalized.get("purpose") or self.workspace.purpose,
            )
        except ContextBudgetError as exc:
            raise MemoryViewError(
                "CONTEXT_BUDGET_TOO_SMALL",
                str(exc),
                data={"required_bytes": exc.required_bytes, "budget_bytes": budget},
            ) from exc
        finally:
            kernel.close()
        response: dict[str, Any] = {
            "context_version": "shared-task-context@1",
            "selector_version": CONTEXT_SELECTOR_VERSION,
            "request": normalized,
            "kernel_state_root": projection["state_root"],
            "ledger_sequence": projection["ledger"]["head_sequence"],
            "core_context": core,
            "task_context": {"records": [], "scenarios": [], "skills": []},
            "selection_trace": [],
            "budget": {
                "budget_bytes": budget,
                "included_bytes": 0,
                "omitted": 0,
                "trace_omitted": 0,
            },
        }
        candidates: list[tuple[str, str, int, dict[str, Any], list[str]]] = []
        for score, identifier, record, reasons in scored:
            candidates.append(("record", identifier, score, _depth_record(record, normalized["depth"]), reasons))
        for score, identifier, artifact, reasons in scenarios:
            candidates.append(("scenario", identifier, score, artifact, reasons))
        for skill in skills:
            candidates.append(
                (
                    "skill",
                    skill["skill_id"],
                    int(skill["selection_score"]),
                    skill,
                    ["approved shared Skill matched task terms"],
                )
            )
        candidates.sort(key=lambda item: (-item[2], item[0], item[1]))
        mandatory_refs = references
        for kind, identifier, score, document, reasons in candidates:
            destination = {
                "record": response["task_context"]["records"],
                "scenario": response["task_context"]["scenarios"],
                "skill": response["task_context"]["skills"],
            }[kind]
            candidate_entry = {"id": identifier, "score": score, "value": document}
            destination.append(candidate_entry)
            included_trace = {
                "id": identifier,
                "kind": kind,
                "included": True,
                "reasons": reasons,
            }
            response["selection_trace"].append(included_trace)
            size = _stabilized_context_size(response)
            if size > budget and identifier not in mandatory_refs:
                destination.pop()
                response["selection_trace"].pop()
                response["budget"]["omitted"] += 1
                omitted_trace = {
                    "id": identifier,
                    "kind": kind,
                    "included": False,
                    "reasons": [*reasons, "budget exceeded"],
                }
                response["selection_trace"].append(omitted_trace)
                if _stabilized_context_size(response) > budget:
                    response["selection_trace"].pop()
                    response["budget"]["trace_omitted"] += 1
                continue
            if size > budget and identifier in mandatory_refs:
                destination.pop()
                response["selection_trace"].pop()
                raise MemoryViewError(
                    "CONTEXT_REFERENCE_EXCEEDS_BUDGET",
                    f"Explicit reference {identifier} cannot fit in the context budget.",
                )
        final_size = _stabilized_context_size(response)
        if final_size > budget:
            raise MemoryViewError(
                "CONTEXT_BUDGET_TOO_SMALL",
                "Core context and required metadata exceed the context budget.",
                data={"required_bytes": final_size, "budget_bytes": budget},
            )
        response.pop("context_hash", None)
        response["context_hash"] = sha256_json(
            {key: value for key, value in response.items() if key != "context_hash"}
        )
        # The placeholder used during stabilization has the exact same length
        # as a SHA-256 value, so the final serialized response remains capped.
        response["budget"]["included_bytes"] = len(
            canonical_json(response).encode("utf-8")
        )
        if response["budget"]["included_bytes"] > budget:
            raise MemoryViewError(
                "CONTEXT_BUDGET_TOO_SMALL",
                "Final context serialization exceeds the context budget.",
                data={
                    "required_bytes": response["budget"]["included_bytes"],
                    "budget_bytes": budget,
                },
            )
        return response


def normalize_context_request(value: Mapping[str, Any]) -> dict[str, Any]:
    hints = dict(value.get("hints") or {})
    forbidden = sorted(set(hints).intersection({"agent_id", "model", "profile"}))
    if forbidden:
        raise MemoryViewError(
            "AGENT_PARTITION_HINT_FORBIDDEN",
            "Agent/model/profile cannot partition Shared Mind: " + ", ".join(forbidden),
        )
    return {
        "object_type": "CONTEXT_REQUEST",
        "request_version": "context-request@1",
        "task": str(value.get("task") or "").strip(),
        "purpose": value.get("purpose"),
        "query": value.get("query"),
        "references": sorted(set(str(item) for item in value.get("references", []))),
        "depth": str(value.get("depth") or "DETAIL").upper(),
        "budget_bytes": value.get("budget_bytes"),
        "budget_tokens": value.get("budget_tokens"),
        "hints": hints,
    }


def _effective_budget(request: Mapping[str, Any]) -> int:
    byte_budget = request.get("budget_bytes")
    token_budget = request.get("budget_tokens")
    if byte_budget is not None and token_budget is not None:
        return min(int(byte_budget), int(token_budget) * 4)
    if byte_budget is not None:
        return int(byte_budget)
    if token_budget is not None:
        return int(token_budget) * 4
    return DEFAULT_CONTEXT_BUDGET_BYTES


def _stabilized_context_size(response: dict[str, Any]) -> int:
    """Return final serialized size using a fixed-length context hash placeholder."""

    preview = copy.deepcopy(response)
    preview["context_hash"] = "sha256:" + "0" * 64
    previous = -1
    for _ in range(8):
        size = len(canonical_json(preview).encode("utf-8"))
        preview["budget"]["included_bytes"] = size
        if size == previous:
            break
        previous = size
    response["budget"]["included_bytes"] = preview["budget"]["included_bytes"]
    response["context_hash"] = preview["context_hash"]
    return len(canonical_json(preview).encode("utf-8"))


def _record_score(
    record: Mapping[str, Any], terms: set[str], references: set[str]
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if record["object_id"] in references:
        score += 10_000
        reasons.append("explicit reference")
    haystack = " ".join(
        [
            str(record["title"]),
            str(record["summary"]),
            canonical_json(record["document"]),
        ]
    ).casefold()
    matches = sorted(term for term in terms if term in haystack)
    if matches:
        score += len(matches) * 20
        reasons.append("matched task terms: " + ", ".join(matches[:8]))
    priority = record["document"].get("priority") if isinstance(record["document"], Mapping) else None
    if priority in {"P0", "P1"}:
        score += 15 if priority == "P0" else 8
        reasons.append(f"{priority} work priority")
    if record["kind"] == "CONFLICT" and record["status"] == "OPEN":
        score += 30
        reasons.append("open fact conflict")
    if record["kind"] in {"DECISION_RECORD", "OPEN_QUESTION", "WORK_ITEM"}:
        score += 3
    return score, reasons


def _artifact_score(
    artifact: Mapping[str, Any], terms: set[str], references: set[str]
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if artifact["artifact_id"] in references:
        score += 10_000
        reasons.append("explicit reference")
    haystack = f"{artifact['title']} {canonical_json(artifact['document'])}".casefold()
    matches = sorted(term for term in terms if term in haystack)
    if matches:
        score += len(matches) * 10
        reasons.append("scenario matched task terms: " + ", ".join(matches[:8]))
    return score, reasons


def _depth_record(record: Mapping[str, Any], depth: str) -> dict[str, Any]:
    base = {
        "object_id": record["object_id"],
        "kind": record["kind"],
        "title": record["title"],
        "summary": record["summary"],
        "status": record["status"],
        "source_revision_ids": record["source_revision_ids"],
        "related_ids": record["related_ids"],
        "projection_ref": record["projection_ref"],
    }
    if depth in {"DETAIL", "EVIDENCE"}:
        base["document"] = record["document"]
    if depth == "EVIDENCE" and "evidence" in record:
        base["evidence"] = record["evidence"]
    return base


def _terms(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN.findall(value) if len(token) > 1}


def _object_text(value: Mapping[str, Any]) -> str:
    if value.get("kind") == "entity":
        return str(value.get("entity_id"))
    return str(value.get("value"))


def _related_ids(document: Mapping[str, Any]) -> list[str]:
    ids: set[str] = set(document.get("related_claim_ids", []))
    ids.update(document.get("related_source_revision_ids", []))
    for ref in document.get("related_objects", []):
        if isinstance(ref, Mapping) and isinstance(ref.get("record_id"), str):
            ids.add(ref["record_id"])
    replacement = document.get("replaced_by_decision_id")
    if isinstance(replacement, str):
        ids.add(replacement)
    return sorted(ids)


def _scenario_summary(records: Sequence[Mapping[str, Any]]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[str(record["kind"])] += 1
    return ", ".join(f"{kind}={counts[kind]}" for kind in sorted(counts)) or "No active records"


def _find_ids(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (key == "id" or key.endswith("_id")) and isinstance(item, str):
                yield item
            yield from _find_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _find_ids(item)


__all__ = [
    "CONTEXT_SELECTOR_VERSION",
    "MEMORY_BUILDER_VERSION",
    "ContextRouter",
    "MemoryViewBuilder",
    "MemoryViewError",
    "normalize_context_request",
]
