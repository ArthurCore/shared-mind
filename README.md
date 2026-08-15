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

The kernel provides the trusted factual and project-state boundary:

- immutable, content-hashed source revisions;
- factual Claims with byte-range EvidenceLinks;
- durable `FACT_CONFLICT` records instead of silent truth selection;
- stale-write rejection through `TRANSACTION_CONFLICT`;
- Decision, OpenQuestion, and WorkItem lifecycles;
- schema-validated, idempotent Proposal commits;
- an append-only ledger, receipts, verification, and deterministic replay;
- deterministic Markdown/JSON projections and structured query; and
- JSON CLI plus an optional local stdio MCP adapter.

The product layer builds on that boundary without becoming a competing source
of factual truth:

- bulk file, repository, code, and JSONL conversation ingest;
- deterministic and policy-gated model extraction into reviewable DraftProposals;
- Scenario, Core Context, Wiki/link, retrieval, and code views that can be deleted
  and rebuilt from canonical state;
- shared, versioned Skills with a separate idempotent product
  proposal/receipt/audit boundary, testing, approval, revision, export, and replay;
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
      ONE canonical Shared Mind kernel ledger
        |                 |                 |
        |                 |                 +-- Decisions / Questions / Work
        |                 +-------------------- Claims / Evidence / Conflicts
        +-------------------------------------- immutable Sources
                  |
                  +------------------------------+
                  |                              |
                  v                              v
  disposable Scenario / Core /        ProductMutationProposal
       retrieval / code views                    |
                  |                              v
                  |                   shared versioned Skills
                  +---------------+--------------+
                                  |
                                  v
             ContextRequest(task/query/ref/budget)
                                  |
                                  v
                deterministic context for any client
```

The kernel database owns factual/project truth and its append-only change
history. The product database owns staging, disposable indexes/views,
telemetry, and shared procedural Skill state. Skill changes use a versioned,
idempotent product proposal with receipts, an audit hash chain, and replay
verification; they do not create Agent-specific memory stores and they do not
rewrite the kernel's factual ledger. See
[Product architecture](docs/product-architecture.md) for the full trust model.

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

Search input is literal Unicode text under `retrieval-index@2`; task IDs such as
`DEV-088`, dotted versions, operators, quotes, and punctuation are never
interpreted as SQLite FTS syntax. Python, CLI, and product MCP responses expose
the retrieval version with the ordered results.

See the [Product guide](docs/product-guide.md) for the complete workflow.

## Authority and safety model

- Source bytes and their hashes are evidence authority.
- The append-only kernel ledger is factual/project change authority.
- Materialized kernel tables are replayable current factual/project state.
- Product Drafts do not mutate canonical state.
- Scenario, Core Context, Wiki, retrieval, and CodeGraph data are disposable
  views, not truth.
- LLM output cannot write canonical memory directly.
- An active factual Claim requires verified EvidenceLink bytes.
- Contradictory Claims remain visible in an open `FACT_CONFLICT`.
- A stale non-commutative kernel Proposal is rejected without advancing the ledger.
- Skills are shared procedural state governed by product proposals, receipts,
  audit hashes, version guards, and replay; they are not copied into
  Agent-specific memory stores.
- A Skill must have explicit passing test evidence before approval.
- Local mode is provider-neutral; embeddings and model extractors are optional.
- The product web server rejects non-loopback bindings.

Current kernel writes use schema `1.3.0`; frozen 1.0–1.2 history remains
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

Shared-state continuity evaluation is available through ProductService, the
`shared-mind-product metrics` commands, and the Product MCP
`continuity_evaluate` tool. DEV-082~086 measure zero relearning, memory
pollution, lifecycle, conflict-resolution preservation, and context quality;
their reports are reproducible evidence rather than canonical truth. See
[the evaluation contract](docs/DEV-082-086-continuity-evaluations.md) and
[the RED/GREEN dogfooding evidence](docs/testing/dev-082-086-continuity-evaluations.tdd.md).

The earlier kernel baseline is retained in
[GitHub Actions run 31555504041](https://github.com/ArthurCore/shared-mind/actions/runs/31555504041).
The DEV-081 product implementation branch passed all eight hosted CI jobs in
[GitHub Actions run 31856420461](https://github.com/ArthurCore/shared-mind/actions/runs/31856420461),
including Python 3.11–3.13 branch coverage, three-OS determinism, quality and
security gates, and fresh base/MCP wheel installation.

The checked-in [DEV-021 benchmark evidence](benchmarks/results/dev-021-2026-08-11.md)
retains the historical schema 1.2 performance and migration baseline. The
[DEV-089 certification](benchmarks/results/dev-089-schema13-2026-08-15.md)
adds fresh schema 1.3 history-heavy and hot-active 100k fixtures, complete
verify/replay parity, a strict content-addressed result schema, and 50-sample
context p95 evidence. Product-level cold-start, routing, retrieval, Skill reuse,
and integrity evaluations are executable in the product test suite.

## License

Shared Mind is licensed under the
[BSD 3-Clause License](LICENSE) (`BSD-3-Clause`). Redistribution, modification,
and commercial use are permitted when the copyright notice, license conditions,
and disclaimer are retained. The names of the copyright holder and contributors
may not be used to endorse derived products without prior written permission.
