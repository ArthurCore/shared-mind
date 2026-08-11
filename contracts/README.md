# Shared Mind — Atlas Kernel Contract v1

This package turns the Atlas vertical slice into an executable data contract.
It deliberately models a narrow, deterministic knowledge domain rather than
accepting arbitrary predicates.

## Files

- `atlas-predicate-registry.v1.json` — the allowed Atlas predicates and their
  semantic conflict rules.
- `shared-mind-kernel.schema.v1.json` — JSON Schema Draft 2020-12 definitions
  for the registry, source revisions, claims, evidence, proposals, conflicts,
  ledger entries, and decision receipts.
- `atlas-conformance-fixtures.v1.json` — three input scenarios with expected
  semantic outcomes.
- `atlas-runbook.fixture.md` — immutable source bytes referenced by the
  fixtures; its content and excerpt hashes are real, not placeholders.
- `validate_contract.py` — validates the schema, registry, and fixture objects.

## Normative boundary

JSON Schema validates shape. The kernel MUST additionally enforce the semantic
rules below in the listed order. A proposal passing JSON Schema is not yet
eligible to commit.

1. Recompute every `content_hash`, `excerpt_hash`, `proposition_hash`, collection
   digest, state root, proposal hash, ledger hash, and conflict member digest.
2. Resolve the predicate from the exact registry version named by the proposal.
3. Enforce predicate subject/object types, allowed scope fields, required
   non-null qualifiers, and temporal policy.
4. Validate every evidence byte range against the immutable `SourceRevision`;
   the selected bytes and stored excerpt must produce `excerpt_hash`.
5. Require each new Claim to have at least the predicate's minimum evidence
   count in the same atomic proposal. Every initial EvidenceLink must name that
   Claim.
6. Derive mandatory reads and guards from operations. Caller-supplied reads and
   guards may strengthen but never weaken kernel requirements.
7. Evaluate aggregate versions, collection digests, and guards inside the same
   write transaction used for the append.
8. Simulate all operations, then apply deterministic conflict rules from the
   pinned registry version.
9. Commit the proposal, events, materialized-state changes, idempotency mapping,
   and receipt atomically. Any failure aborts all mutations.

## Canonical proposition

`proposition_hash` is the SHA-256 digest of RFC 8785-style canonical JSON for
the `proposition` object, prefixed with `sha256:`. The canonical object includes
all four scope keys and both valid-time keys; absent meaning is represented by
JSON `null`, never by omission.

The v1 semantic family is computed from the registry's `family_key_fields`.
For example, `deployment.database_engine@1` is partitioned by subject,
predicate, and `scope.environment`. The object is intentionally excluded so
PostgreSQL and MySQL land in the same exclusive-value family.

## Outcome rules

| Situation | Receipt outcome | Mutation ledger append |
|---|---|---:|
| Valid change, no newly opened conflict | `COMMITTED` | yes |
| Valid assertion opens a fact conflict | `FACT_CONFLICT` | yes |
| Stale read, failed guard, or phantom member change | `TRANSACTION_CONFLICT` | no |
| Shape, reference, normalization, policy, or evidence failure | `VALIDATION_ERROR` | no |

An incoming `ASSERT_CLAIM` is not rejected merely because it contradicts an
active Claim. Both assertions are preserved and an OPEN conflict episode is
emitted. A stale `SUPERSEDE_CLAIM`, `RETRACT_CLAIM`, or `RESOLVE_CONFLICT` is
rejected as a transaction conflict.

## Conflict rules in v1

- `OPPOSITE_POLARITY`: same family and object, overlapping valid time, opposite
  polarity.
- `EXCLUSIVE_OBJECT`: cardinality `ONE`, same family, overlapping valid time,
  different positive objects.
- `TEMPORAL_OVERLAP`: the overlap condition used by exclusive temporal values;
  adjacent non-overlapping intervals do not conflict.

`migration.target_database_engine@1` is distinct from the current deployment
database predicate. Therefore “planned migration to MySQL” does not contradict
“production currently uses PostgreSQL.”

## Mandatory operation guards

| Operation | Kernel-derived requirement |
|---|---|
| `REGISTER_SOURCE_REVISION` | revision ID is unused, or identical content hash for an idempotent replay |
| `ASSERT_CLAIM` | source revisions exist; evidence and predicate policy pass |
| `ATTACH_EVIDENCE` | target Claim version/status and source hash are current |
| `SUPERSEDE_CLAIM` | target Claim is `ACTIVE` at the observed lifecycle version |
| `RETRACT_CLAIM` | target Claim is `ACTIVE` at the observed lifecycle version; actor is authorized |
| `RESOLVE_CONFLICT` | conflict is `OPEN`; member digest and resolution epoch match |

## Validation

Run:

```bash
python3 validate_contract.py
```

The validator always performs dependency-free registry consistency, canonical
proposition hash, source-content hash, and evidence-range/hash checks. If Python
`jsonschema` is installed, it additionally checks Draft 2020-12 schema validity
and validates the registry plus every typed fixture object. The
expected commit outcomes in the fixtures are conformance expectations for the
future kernel, not results produced by the shape validator.
