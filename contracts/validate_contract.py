#!/usr/bin/env python3
"""Validate the Shared Mind Atlas v1 contract and its typed fixtures."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ModuleNotFoundError:  # Optional enhanced validation.
    Draft202012Validator = None
    FormatChecker = None


ROOT = Path(__file__).resolve().parent


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


def main() -> None:
    schema = load_json("shared-mind-kernel.schema.v1.json")
    registry = load_json("atlas-predicate-registry.v1.json")
    fixtures = load_json("atlas-conformance-fixtures.v1.json")

    typed_objects = fixtures["typed_objects"]
    enhanced = Draft202012Validator is not None
    if enhanced:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(registry)
        for fixture in typed_objects:
            validator.validate(fixture["object"])

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

    for fixture in typed_objects:
        candidate = fixture["object"]
        if candidate.get("object_type") != "PROPOSAL":
            continue
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

    mode = "Draft 2020-12 + registry" if enhanced else "registry consistency"
    print(f"OK ({mode}): {len(predicate_keys)} predicates + {len(typed_objects)} typed fixtures")


if __name__ == "__main__":
    main()
