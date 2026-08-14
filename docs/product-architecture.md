# Shared Mind Product Architecture

| Item | Value |
|---|---|
| Architecture version | 1.0.0 |
| Package baseline | 0.3.0 |
| Kernel write schema | 1.3.0 |
| Product API | `shared-mind-product@1` |
| Principle | Project has state. Agents come and go. |

## 1. Architectural invariant

Shared Mind does not create persistent memory partitions for Agents, models, or
sessions. Every client observes one canonical project state.

```text
Agent A memory != Agent B memory      forbidden
Shared Mind(A) == Shared Mind(B)      required
Context(task A) != Context(task B)    allowed
```

A role or model name may describe the current task, but it is never a storage
partition key. An approved change made through one client becomes visible to
all later clients through the same Shared State.

## 2. Two persistence responsibilities

### 2.1 Kernel database: canonical authority

The kernel database owns:

- immutable `SourceRevision` records and content hashes;
- `Claim` and `EvidenceLink` records;
- `Conflict` lifecycle;
- `DecisionRecord`, `OpenQuestion`, and `WorkItem` lifecycle;
- accepted Proposals, rejected receipts, and the append-only ledger;
- materialized state that can be rebuilt through deterministic replay.

All canonical mutations cross the kernel Proposal boundary. Public SQLite DML
and DDL are denied by the kernel authorizer and append-only triggers.

### 2.2 Product database: staging and rebuildable product state

`.shared-mind/product.sqlite3` owns:

- ingest batches and item manifests;
- reviewable DraftProposals;
- derived Scenario, Atomic Map, and Core Context artifacts;
- shared versioned Skill workflow;
- retrieval documents, links, code symbols, and code edges;
- minimal product telemetry;
- product mutation Proposals/receipts and a hash-chained audit stream.

This database does not become a second factual authority. Factual and project
work-state candidates are converted to kernel Proposals before commit. Product
Skills use their own versioned Proposal/receipt/replay contract because they are
procedural assets rather than factual Claims.

## 3. Write paths

### 3.1 Documents and conversations

```text
input bytes
  -> bounded ingest manifest
  -> immutable kernel SourceRevision
  -> deterministic/model extractor
  -> DraftProposal staging
  -> review/edit/reject
  -> kernel Proposal validation and commit
  -> ledger/receipt
```

A model extractor requires an explicit remote-policy `ALLOW` decision. Every
candidate retains extractor, model, prompt, parameter, timestamp, and input
source revision provenance. A failed or rejected Draft does not advance the
kernel ledger.

### 3.2 Skills

```text
task trace or reviewed definition
  -> shared SkillRecord DRAFT
  -> explicit passing test evidence
  -> TESTED
  -> reviewer approval
  -> APPROVED
  -> revise or deprecate through optimistic version guards
```

Skills have one shared identity and monotonically increasing versions. They are
never copied into Agent-specific stores. A Skill package includes the Skill
document, resource fingerprints, validation metadata, and deterministic ZIP
metadata.

## 4. Read paths

### 4.1 Atomic state

`MemoryViewBuilder.atomic_records()` normalizes kernel SourceRevision, Claim,
Conflict, Decision, OpenQuestion, and WorkItem records into a common read
envelope. This is a projection, not a new authority.

### 4.2 Derived views

- `ATOMIC_MAP`: project-wide normalized atomic state.
- `SCENARIO`: deterministic project, subject, decision-thread, incident, and
  workstream views over member object IDs.
- `CORE_CONTEXT`: project purpose, active decisions, critical constraints, open
  conflicts/questions, and current work derived from the current kernel state.

Scenario dependency digests use member object digests rather than the global
state root, so unrelated changes do not rebuild unrelated Scenarios. Project-
wide Atomic/Core artifacts intentionally track the whole state.

`ProductService.verify()` rebuilds managed views in a temporary product store
and compares semantic documents and dependency digests with READY artifacts.
External SQL tampering or stale READY artifacts therefore fail verification.

### 4.3 Task-aware context

A `ContextRequest` contains:

- task and optional purpose/query;
- explicit object/source/file references;
- desired depth (`SUMMARY`, `DETAIL`, `EVIDENCE`);
- hard byte/token budget;
- optional non-partitioning hints.

The selector assembles:

```text
Shared Core Context
  + task-relevant atomic records
  + relevant Scenarios
  + APPROVED shared Skills
  + drill-down pointers
  + selection/omission trace
```

Model or Agent identity hints that attempt to partition memory are rejected.
The final serialized response is measured against the hard budget; explicit
references are either preserved or the request fails closed.

## 5. Retrieval and code understanding

The dependency-free default is local lexical retrieval:

- SQLite FTS5/BM25 when available;
- deterministic token-count fallback otherwise;
- optional vector ranker fused through RRF without changing correctness;
- source/evidence/provenance metadata on results.

Python code indexing records modules, classes, functions, and methods, plus
`CALLS` and non-call `REFERENCES` edges. The index and link graph can be deleted
and rebuilt from immutable source revisions. On-demand tools expose search,
source spans, artifacts, Skills, symbols, impact paths, and links without
injecting the whole store into a prompt.

## 6. Interfaces

The existing kernel interfaces remain stable. Product functionality is exposed
through separate surfaces that reuse `ProductService`:

- `shared-mind-product` JSON CLI;
- `shared-mind-product-mcp` optional stdio MCP;
- `shared-mind-web` loopback-only HTTP UI/API;
- `shared-mind context --task ...` compatibility path.

The kernel MCP allowlist is not widened. Product MCP paths are resolved inside
one fixed workspace and reject absolute or escaping paths.

## 7. Integrity, backup, and recovery

Product verification combines:

- kernel ledger/hash/state verification;
- product audit hash-chain verification;
- shared Skill Proposal replay parity;
- canonical rebuild comparison for managed derived views;
- provenance completeness checks.

Backup creates a validated ZIP containing kernel/product SQLite snapshots,
workspace configuration, registry, sources/blobs, projections, and a manifest
of sizes and hashes. Restore stages extraction, rejects duplicate, undeclared,
symlink, directory, traversal, oversized, and hash-mismatched entries, then
verifies kernel state root and product state hash before completing.

## 8. Trust boundary

Local filesystem and database ownership remain the outer trust boundary. The
system detects ordinary corruption and uncoordinated database tampering, but a
filesystem owner capable of replacing databases, source bytes, contracts, and
all verification metadata together remains outside the local threat model.
Remote identity, origin authentication, and network disclosure transport are
separate adapter responsibilities.
