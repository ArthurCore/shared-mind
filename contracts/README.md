# Shared Mind — Atlas Kernel Contract v1

This package turns the Atlas vertical slice into an executable data contract.
It deliberately models a narrow, deterministic knowledge domain rather than
accepting arbitrary predicates.

## Files

- `atlas-predicate-registry.v1.json` — the allowed Atlas predicates and their
  semantic conflict rules.
- `shared-mind-kernel.schema.v1.json` — JSON Schema Draft 2020-12 definitions
  for the registry, source revisions, claims, evidence, proposals, conflicts,
  continuity records, ledger entries, and decision receipts.
- `shared-mind-read.schema.v1.json` — closed Draft 2020-12 contract for
  `structured-query@1` inputs, deterministic query results, and advisory
  `rebase-hint@1` values.
- `atlas-conformance-fixtures.v1.json` — typed objects, negative schema cases,
  and semantic scenarios with expected outcomes.
- `atlas-runbook.fixture.md` — immutable source bytes referenced by the
  fixtures; its content and excerpt hashes are real, not placeholders.
- `validate_contract.py` — validates the schema, registry, and fixture objects.

## Normative boundary

JSON Schema validates shape. The kernel MUST additionally enforce the semantic
rules below in the listed order. A proposal passing JSON Schema is not yet
eligible to commit.

1. Recompute every `content_hash`, `excerpt_hash`, `proposition_hash`, collection
   digest, state root, proposal hash, ledger hash, and conflict member digest.
2. Resolve the predicate from the exact registry version and canonical registry
   content hash named by the proposal.
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
9. Resolve every continuity-record reference and enforce the lifecycle and
   optimistic-concurrency rules below. New records start at version 1; every
   accepted lifecycle mutation increments exactly once.
10. Commit the proposal, events, materialized-state changes, idempotency mapping,
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

## Ledger and receipt documents

Write schema `1.3.0` stores a canonical, schema-valid `LedgerEntry` document
beside normalized SQLite columns. Its `entry_hash` is the SHA-256 digest of
canonical JSON for exactly this preimage; the hash itself, derived `entry_id`,
and constant `object_type` are deliberately excluded:

```text
{
  seq, prev_hash, proposal_hash, pre_state_root, post_state_root,
  versions, events, committed_at
}
```

The verifier recomputes that preimage and also checks byte-canonical document
serialization and parity with every normalized column. A `DecisionReceipt`
records the ledger head before and after evaluation. Accepted outcomes link to
the exact `LedgerEntry`; rejected outcomes keep identical before/after heads
and do not advance the ledger. If an attempted value is not a JSON Proposal,
its stable diagnostic fingerprint occupies `proposal_hash` while invalid or
missing proposal/idempotency identifiers are represented as JSON `null`, not
fabricated canonical IDs.

Schema `1.3.0` also requires `DecisionReceipt.proposer`; the value is an
`ActorRef` when the attempted input supplies a representable proposer and JSON
`null` otherwise. Required-but-nullable provenance makes the absence explicit
without inventing an actor. The verifier checks the receipt document,
normalized proposer column, and linked accepted Proposal together, including
coordinated document/column tampering. Rejected receipts have no linked ledger
Proposal; their canonical document/normalized-column parity and historical
head/state-root position are verified, while database-owner forensic rewrites
remain outside the local cryptographic trust boundary.

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

## Read contract

The read contract is deliberately separate from the write schema. A
`StructuredQuery` filters the public projection by object kind, exact ID,
title substring, predicate, source/source revision, and lifecycle status. Filter
categories are ANDed, values within a category are ORed, and results have stable
kind/ID ordering, pagination, projection references, bounded summaries, and an
optional full record. Queries never become mutation authority.

A `RebaseHint` is returned beside—not inside—the canonical
`DecisionReceipt` for an interpretable `TRANSACTION_CONFLICT`. It records the
observed state/head and replacement preconditions, but pins
`advisory: true`, `safe_to_auto_apply: false`, and
`recommended_action: REVIEW_AND_REBUILD`. A caller must refresh, review, and
construct a new Proposal; the hint is not a write operation.

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
| `ATTACH_EVIDENCE` | target Claim is `ACTIVE`; selector/excerpt hashes are revalidated against the named immutable source revision (concurrent attaches are commutative and do not require an observed Claim version) |
| `SUPERSEDE_CLAIM` | target Claim is `ACTIVE` at the observed lifecycle version |
| `RETRACT_CLAIM` | target Claim is `ACTIVE` at the observed lifecycle version; actor is authorized |
| `RESOLVE_CONFLICT` | conflict is `OPEN`; member digest and resolution epoch match |
| `RECORD_DECISION` | new DecisionRecord ID; initial `ACTIVE` status and version 1 |
| `SUPERSEDE_DECISION` | target DecisionRecord is `ACTIVE` at the observed version; replacement is a new `ACTIVE` version-1 record |
| `OPEN_QUESTION` | new OpenQuestion ID; initial `OPEN` status and version 1 |
| `ANSWER_QUESTION` | target OpenQuestion is `OPEN` at the observed version; answer reference resolves |
| `DROP_QUESTION` | target OpenQuestion is `OPEN` at the observed version |
| `CREATE_WORK_ITEM` | new WorkItem ID; initial `TODO` status and version 1 |
| `UPDATE_WORK_ITEM_STATUS` | target WorkItem status and version match; transition is allowed; `BLOCKED` has a blocker and every other status has none |

## Continuity records

Continuity records are canonical, ledger-backed state rather than generated
handoff snapshots. `DecisionRecord` preserves a conclusion, rationale,
alternatives, and related source/Claim IDs. `OpenQuestion` preserves its
context and either an answer with a canonical record reference or a drop
rationale. `WorkItem` preserves priority, current status, and an explicit
blocker only while blocked. Cross-record links use a typed `RecordRef` instead
of an untyped string.

| Record | Lifecycle |
|---|---|
| `DecisionRecord` | `ACTIVE` -> `SUPERSEDED` or `REVERSED` |
| `OpenQuestion` | `OPEN` -> `ANSWERED` or `DROPPED` |
| `WorkItem` | `TODO`, `DOING`, `BLOCKED`, `DONE`, `DROPPED`; allowed transition edges are checked semantically |

The schema makes each lifecycle state internally complete. An active decision
cannot already name its replacement; an answered question must carry its
answer and reference; a dropped question must carry its drop record; and a
blocked work item must carry a non-empty blocker. Creation operations further
constrain their embedded records to the initial lifecycle state and version 1.

Every continuity mutation requires one matching aggregate read plus matching
status and version guards. These fixture guards document conformance inputs;
the kernel MUST derive the same preconditions from the operation and MUST NOT
trust their presence alone. The ledger event union includes a replayable event
for all seven continuity operations. Mutation events carry previous and new
versions so version progression can be verified during replay.

## Version and migration boundary

The current write schema is `1.3.0`, the current projection is
`markdown-projection@3`, and structured reads use `structured-query@1`. Handoff
contexts use `handoff-context@3` and `context-selection@3`. New proposals must
also pin the registry content hash.
Schema `1.0.0` ledgers from baseline commit `3c3cdf0` remain readable through a
separate legacy envelope/event/state-root reducer. Opening such a database does
not rewrite its hashed ledger fields. Replay verifies the pre-ledger source
bytes and content hashes and uses them as the legacy origin state because that
baseline registered sources outside the ledger.

Schema `1.1.0` full-event rows are readable and replayable, but rows written
before exact documents were introduced may still have a null document. Legacy
rows cannot truthfully acquire event-complete `LedgerEntry` or historical
`DecisionReceipt` documents after the fact. Their document columns therefore
remain null and public exact-contract access reports
`LEGACY_*_CONTRACT_INCOMPLETE`; they are never silently presented as current
contract objects. Schema `1.2.0` introduced exact receipt documents without the
new proposer member. A short transitional 1.2 writer also emitted
proposer-bearing receipt documents before the version was corrected to 1.3.
Both historical 1.2 shapes are recognized and preserved byte-for-byte rather
than rewritten. A current `1.3.0` entry may follow 1.0, 1.1, or either 1.2
receipt variant, crossing from the four-table legacy state-root domain to the
continuity-inclusive current domain when necessary. All four readable versions
remain version-dispatched, and mixed 1.2/1.3 receipt history must verify and
replay to the same final head, receipt stream, counts, and state root.

## Validation

Run:

```bash
python3 contracts/validate_contract.py
```

The validator always performs dependency-free registry consistency, canonical
proposition hash, source-content hash, evidence-range/hash checks, continuity
operation coverage, and continuity mutation read/guard checks. If Python
`jsonschema` is installed, it additionally checks Draft 2020-12 schema validity
and validates the registry, every typed fixture object, and every negative
schema case. The expected commit outcomes and deliberately modified semantic
cases in the fixtures are conformance expectations for the runtime kernel, not
results produced by the shape validator.
