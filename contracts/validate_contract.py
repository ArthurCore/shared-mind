#!/usr/bin/env python3
"""Validate the Shared Mind Atlas v1 contract and conformance fixtures."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ModuleNotFoundError:  # Optional enhanced validation.
    Draft202012Validator = None
    FormatChecker = None


ROOT = Path(__file__).resolve().parent

CONTINUITY_OPERATIONS = {
    "RECORD_DECISION",
    "SUPERSEDE_DECISION",
    "OPEN_QUESTION",
    "ANSWER_QUESTION",
    "DROP_QUESTION",
    "CREATE_WORK_ITEM",
    "UPDATE_WORK_ITEM_STATUS",
}

CONTINUITY_MUTATIONS = {
    "SUPERSEDE_DECISION": {
        "target_field": "target_decision_id",
        "aggregate_type": "DECISION_RECORD",
        "status_guard": "DECISION_STATUS_EQ",
        "version_guard": "DECISION_VERSION_EQ",
        "guard_id_field": "decision_id",
        "expected_status": "ACTIVE",
    },
    "ANSWER_QUESTION": {
        "target_field": "target_question_id",
        "aggregate_type": "OPEN_QUESTION",
        "status_guard": "QUESTION_STATUS_EQ",
        "version_guard": "QUESTION_VERSION_EQ",
        "guard_id_field": "question_id",
        "expected_status": "OPEN",
    },
    "DROP_QUESTION": {
        "target_field": "target_question_id",
        "aggregate_type": "OPEN_QUESTION",
        "status_guard": "QUESTION_STATUS_EQ",
        "version_guard": "QUESTION_VERSION_EQ",
        "guard_id_field": "question_id",
        "expected_status": "OPEN",
    },
    "UPDATE_WORK_ITEM_STATUS": {
        "target_field": "target_work_item_id",
        "aggregate_type": "WORK_ITEM",
        "status_guard": "WORK_ITEM_STATUS_EQ",
        "version_guard": "WORK_ITEM_VERSION_EQ",
        "guard_id_field": "work_item_id",
        "expected_status": None,
    },
}


def load_json(name: str) -> object:
    with (ROOT / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def sha256_prefixed(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    # The fixture uses ASCII keys/values and integer/null values, for which this
    # serialization is byte-equivalent to RFC 8785 canonical JSON.
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_continuity_guards(proposal: dict[str, object]) -> set[str]:
    """Validate fixture-level optimistic-concurrency requirements.

    Runtime kernels MUST derive these requirements independently. This helper
    makes the positive conformance fixtures executable and prevents examples
    from accidentally documenting an unsafe continuity mutation.
    """
    reads = proposal["reads"]
    guards = proposal["guards"]
    operation_names: set[str] = set()
    for operation in proposal["operations"]:
        operation_name = operation["op"]
        if operation_name not in CONTINUITY_OPERATIONS:
            continue
        operation_names.add(operation_name)
        policy = CONTINUITY_MUTATIONS.get(operation_name)
        if policy is None:
            continue

        target_id = operation[policy["target_field"]]
        matching_reads = [
            item
            for item in reads
            if item.get("kind") == "AGGREGATE"
            and item.get("aggregate_type") == policy["aggregate_type"]
            and item.get("aggregate_id") == target_id
        ]
        if len(matching_reads) != 1:
            raise ValueError(
                f"{operation_name} must have exactly one target aggregate read: "
                f"{proposal['proposal_id']}"
            )
        expected_version = matching_reads[0]["expected_version"]
        id_field = policy["guard_id_field"]
        version_guard = [
            item
            for item in guards
            if item.get("op") == policy["version_guard"]
            and item.get(id_field) == target_id
            and item.get("expected_version") == expected_version
        ]
        if len(version_guard) != 1:
            raise ValueError(
                f"{operation_name} must pin the target version in a guard: "
                f"{proposal['proposal_id']}"
            )
        status_guard = [
            item
            for item in guards
            if item.get("op") == policy["status_guard"]
            and item.get(id_field) == target_id
        ]
        if len(status_guard) != 1:
            raise ValueError(
                f"{operation_name} must pin the target lifecycle status: "
                f"{proposal['proposal_id']}"
            )
        expected_status = policy["expected_status"]
        if expected_status is not None and status_guard[0].get("expected_status") != expected_status:
            raise ValueError(
                f"{operation_name} requires target status {expected_status}: "
                f"{proposal['proposal_id']}"
            )
    return operation_names


def main() -> None:
    schema = load_json("shared-mind-kernel.schema.v1.json")
    registry = load_json("atlas-predicate-registry.v1.json")
    fixtures = load_json("atlas-conformance-fixtures.v1.json")

    typed_objects = fixtures["typed_objects"]
    typed_by_name = {item["name"]: item["object"] for item in typed_objects}
    if len(typed_by_name) != len(typed_objects):
        raise ValueError("Typed fixture names must be unique")
    negative_cases = fixtures.get("negative_schema_cases", [])
    semantic_cases = fixtures.get("semantic_cases", [])
    enhanced = Draft202012Validator is not None
    if enhanced:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(registry)
        for fixture in typed_objects:
            validator.validate(fixture["object"])
        for case in negative_cases:
            candidate = copy.deepcopy(typed_by_name[case["base_object"]])
            for field in case["remove_fields"]:
                candidate.pop(field, None)
            candidate.update(copy.deepcopy(case["replace_fields"]))
            if validator.is_valid(candidate):
                raise ValueError(f"Negative fixture unexpectedly passed: {case['name']}")

    for case in semantic_cases:
        references = list(case["given"]) + [case["proposal"]]
        replacement = case.get("replacement_claim_from")
        if replacement is not None:
            references.append(replacement)
        unknown = [name for name in references if name not in typed_by_name]
        if unknown:
            raise ValueError(f"Unknown fixture reference in {case['name']}: {unknown}")

    predicate_keys = [item["key"] for item in registry["predicates"]]
    if len(predicate_keys) != len(set(predicate_keys)):
        raise ValueError("Predicate keys must be unique")

    known_entities = set(registry["entity_types"])
    known_scopes = set(registry["scope_fields"])
    for predicate in registry["predicates"]:
        if not set(predicate["subject_types"]).issubset(known_entities):
            raise ValueError(f"Unknown subject type in {predicate['key']}")
        if predicate["object"]["kind"] == "entity":
            if not set(predicate["object"]["entity_types"]).issubset(known_entities):
                raise ValueError(f"Unknown object entity type in {predicate['key']}")
        allowed = set(predicate["scope"]["allowed_fields"])
        required = set(predicate["scope"]["required_fields"])
        if not allowed.issubset(known_scopes) or not required.issubset(allowed):
            raise ValueError(f"Invalid scope policy in {predicate['key']}")
        rule_kinds = {rule["kind"] for rule in predicate["conflict_rules"]}
        if predicate["cardinality"] == "ONE" and "EXCLUSIVE_OBJECT" not in rule_kinds:
            raise ValueError(f"ONE predicate lacks EXCLUSIVE_OBJECT: {predicate['key']}")
        if predicate["cardinality"] == "MANY" and "EXCLUSIVE_OBJECT" in rule_kinds:
            raise ValueError(f"MANY predicate cannot use EXCLUSIVE_OBJECT: {predicate['key']}")

    source_bytes = (ROOT / "atlas-runbook.fixture.md").read_bytes()
    source_revision = typed_objects[0]["object"]
    if sha256_prefixed(source_bytes) != source_revision["content_hash"]:
        raise ValueError("Fixture source content hash mismatch")

    continuity_operations: set[str] = set()
    for fixture in typed_objects:
        candidate = fixture["object"]
        if candidate.get("object_type") != "PROPOSAL":
            continue
        continuity_operations.update(validate_continuity_guards(candidate))
        for operation in candidate["operations"]:
            claims_and_evidence = []
            if operation["op"] == "ASSERT_CLAIM":
                claims_and_evidence.append((operation["claim"], operation["initial_evidence"]))
            elif operation["op"] == "SUPERSEDE_CLAIM":
                claims_and_evidence.append((operation["replacement_claim"], operation["initial_evidence"]))
            for claim, evidence_links in claims_and_evidence:
                expected = sha256_prefixed(canonical_json_bytes(claim["proposition"]))
                if claim["proposition_hash"] != expected:
                    raise ValueError(f"Proposition hash mismatch for {claim['claim_id']}")
                for link in evidence_links:
                    if link["claim_id"] != claim["claim_id"]:
                        raise ValueError(f"Evidence targets wrong claim: {link['evidence_link_id']}")
                    selector = link["selector"]
                    selected = source_bytes[selector["start_byte"] : selector["end_byte"]]
                    if selected.decode("utf-8") != selector["excerpt"]:
                        raise ValueError(f"Evidence excerpt mismatch: {link['evidence_link_id']}")
                    if sha256_prefixed(selected) != selector["excerpt_hash"]:
                        raise ValueError(f"Evidence hash mismatch: {link['evidence_link_id']}")

    missing_continuity_operations = CONTINUITY_OPERATIONS - continuity_operations
    if missing_continuity_operations:
        raise ValueError(
            "Missing continuity operation fixtures: "
            + ", ".join(sorted(missing_continuity_operations))
        )

    mode = "Draft 2020-12 + registry" if enhanced else "registry consistency"
    print(
        f"OK ({mode}): {len(predicate_keys)} predicates + "
        f"{len(typed_objects)} typed fixtures + {len(negative_cases)} negative cases + "
        f"{len(semantic_cases)} semantic cases + "
        f"{len(continuity_operations)} continuity operations"
    )


if __name__ == "__main__":
    main()
