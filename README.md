# Shared Mind

> **Project has state. Agents come and go.**

Shared Mind is a local-first external cognitive state for carrying sources,
evidence, claims, decisions, questions, conflicts, work state, and reusable
Skills across AI sessions. Codex, Claude, GPT, and other clients do not receive
separate private copies of project memory. They observe one canonical Shared
State and request a deterministic, task-aware view of that state.

```text
Agent A memory != Agent B memory      # forbidden
Shared Mind(A) == Shared Mind(B)      # required
Context(task A) != Context(task B)    # allowed
```

## What it provides

The kernel provides the trusted state boundary:

- immutable, content-hashed source revisions;
- factual Claims with byte-range EvidenceLinks;
- durable `FACT_CONFLICT` records instead of silent truth selection;
- stale-write rejection through `TRANSACTION_CONFLICT`;
- Decision, OpenQuestion, and WorkItem lifecycles;
- schema-validated, idempotent Proposal commits;
- an append-only ledger, receipts, verification, and deterministic replay;
- deterministic Markdown/JSON projections and structured query; and
- JSON CLI plus an optional local stdio MCP adapter.

The product layer builds on that boundary without becoming a second source of
truth:

- bulk file, repository, code, and JSONL conversation ingest;
- deterministic and policy-gated model extraction into reviewable DraftProposals;
- Scenario, Core Context, Wiki/link, retrieval, and code views that can be deleted
  and rebuilt from canonical state;
- shared, versioned Skills with testing, approval, revision, export, and replay;
- deterministic Task-aware Context with selection trace and hard budgets;
- FTS5/BM25 retrieval, optional vector/RRF fusion, Python symbol/reference/call
  indexing, and impact paths;
- cold start, review queues, catalog, telemetry, backup/restore, and product
  quality benchmarks; and
- separate product CLI, MCP, and loopback-only web control surfaces.

## Architecture

```text
Files / conversations / code / task traces
                  |
                  v
        immutable SourceRevisions (L0)
                  |
                  v
        reviewable DraftProposals
                  |
                  v
             Proposal commit
                  |
                  v
      ONE canonical Shared Mind ledger
        |          |          |
        |          |          +-- shared versioned Skills
        |          +------------- Decisions / Questions / Work
        +------------------------ Claims / Evidence / Conflicts
                  |
                  v
  disposable Scenario / Core / retrieval / code views
                  |
                  v
          ContextRequest(task/query/ref/budget)
                  |
                  v
        deterministic context for any client
```

The canonical kernel database owns project truth and change history. The
product database owns staging, disposable indexes/views, Skill workflow, and
telemetry. Factual or work-state changes still cross the kernel Proposal
boundary. See [Product architecture](docs/product-architecture.md) for the full
trust model.

## Quick start

Shared Mind requires Python 3.11 or newer.

```console
$ python3 -m pip install -e .
$ shared-mind init ./memory --purpose "Preserve this project's reasoning across AI sessions."
$ cd ./memory
```

### Cold-start an existing project

```console
$ shared-mind-product cold-start ./project \
    --conversation ./sessions.jsonl \
    --task "Continue implementation"
```

The command performs bounded ingest, deterministic extraction, Proposal commit,
derived-view/index rebuild, and first handoff generation. Model-backed extraction
is never enabled implicitly.

### Review before committing

```console
$ shared-mind-product ingest ./project --conversation ./sessions.jsonl
$ shared-mind-product extract <batch-id>
$ shared-mind-product draft list --batch-id <batch-id>
$ shared-mind-product draft show <draft-id>
$ shared-mind-product draft commit <draft-id>
$ shared-mind-product build all
```

### Request task-aware context

```console
$ shared-mind context \
    --task "Review the authentication migration" \
    --query "auth compatibility" \
    --budget-bytes 32768
```

The same state, request, selector version, and budget produce the same context
regardless of which model or client made the request.

### Search and drill down on demand

```console
$ shared-mind-product search "authentication migration"
$ shared-mind-product tool capabilities
$ shared-mind-product tool read_source_span \
    --arguments '{"revision_id":"revision_...","start_byte":0,"end_byte":200}'
```

See the [Product guide](docs/product-guide.md) for the complete workflow.

## Authority and safety model

- Source bytes and their hashes are evidence authority.
- The append-only kernel ledger is canonical change authority.
- Materialized kernel tables are replayable current state.
- Product Drafts do not mutate canonical state.
- Scenario, Core Context, Wiki, retrieval, and CodeGraph data are disposable
  views, not truth.
- LLM output cannot write canonical memory directly.
- An active factual Claim requires verified EvidenceLink bytes.
- Contradictory Claims remain visible in an open `FACT_CONFLICT`.
- A stale non-commutative Proposal is rejected without advancing the ledger.
- Skills are shared by identity and version; they are not copied into
  Agent-specific memory stores.
- A Skill must have explicit passing test evidence before approval.
- Local mode is provider-neutral; embeddings and model extractors are optional.
- The product web server rejects non-loopback bindings.

Current kernel writes use schema `1.4.0`; frozen 1.0–1.3 history remains
readable and replay-verifiable. The separate product API and product contract
use `shared-mind-product@1`; package version `0.3.0` introduces the product
layer without rewriting frozen kernel history.

## Interfaces

```text
shared-mind                 Kernel CLI and task-aware context compatibility path
shared-mind-mcp             Frozen, optional kernel MCP surface
shared-mind-product         Ingest, review, views, Skills, search, governance
shared-mind-product-mcp     Separate product MCP surface
shared-mind-web             Loopback-only local control surface
```

Coding agents should start with the
[Coding-agent bootstrap](docs/agent-bootstrap.md). Product workflows and exact
command examples are in the [Product guide](docs/product-guide.md).

## Repository layout

```text
ROADMAP.md                         Product goals and implementation tracking
contracts/                         Kernel/read/product schemas and fixtures
docs/SRS.md                        Kernel and continuity baseline SRS
docs/SRS-product-v1.md             Product-layer requirements and traceability
docs/product-architecture.md       One Shared State architecture and trust model
docs/product-guide.md              Operator and agent workflow guide
src/shared_mind/                    Kernel and product implementation
tests/                              Conformance, regression, product, security tests
benchmarks/                         Deterministic scale benchmark evidence
evals/                              Product-continuity scorer and retained evidence
AGENTS.md                           Contributor invariants
```

## Verify

```bash
python3 contracts/validate_contract.py
python3 contracts/validate_product_contract.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

CI runs the complete suite with branch coverage on Python 3.11, 3.12, and 3.13;
determinism subsets on Linux, macOS, and Windows; compile/lint/type/dependency/
security gates; and fresh base/MCP wheel installation smoke tests. Product
Scenario and retrieval determinism are included in the cross-platform subset.

The earlier kernel baseline is retained in
[GitHub Actions run 31555504041](https://github.com/ArthurCore/shared-mind/actions/runs/31555504041).
Current product-layer evidence is recorded in the branch/PR CI run referenced by
`ROADMAP.md` after all jobs pass.

The checked-in [DEV-021 benchmark evidence](benchmarks/results/dev-021-2026-08-11.md)
continues to cover the 100k-entry kernel context path. Product-level cold-start,
routing, retrieval, Skill reuse, and integrity evaluations are executable in
the product test suite.
