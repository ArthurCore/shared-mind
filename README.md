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
  quality benchmarks;
- fail-open Claude Code hook capture, deterministic observation archives, a
  read-only live observation viewer, and an explicit review queue; and
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

Shared Mind currently installs from a source checkout; this is not a claim that
the package is published to a package index. From the repository root, `uv`
selects a compatible Python 3.11+ runtime and keeps the tool environment
isolated, so no virtual environment activation is required.

```console
$ uv tool install --editable '.[mcp]'
$ shared-mind setup --install-hooks
```

Run `uv tool update-shell` once if the installed commands are not yet on
`PATH`. `setup --install-hooks` finds the current Git project, creates or reuses
its conventional `<project>-memory` sibling, performs the first bounded cold
start exactly once, installs the global Codex `shared-mind-setup` skill, writes
the project-local `.shared-mind/project-binding.json`, and reconciles Claude
Code plus Codex lifecycle hooks. It verifies integrity and returns `SETUP_READY`.

After that one-time command, start Claude Code or Codex from anywhere inside the
same Git project. The working directory selects the nearest Git root; that root's
binding selects exactly one Shared Mind workspace. Session-start hooks inject the
same deterministic 24 KiB EVIDENCE context into Claude and Codex, and
UserPromptSubmit hooks refine context with the actual prompt without searching
neighboring projects. Codex project hooks require the normal trust review before
they run.

Re-running setup reuses the same Shared State and preserves unrelated Claude and
Codex settings. Hook collection is the only fail-open boundary: a malformed or
unavailable hook capture records a non-canonical diagnostic and returns control
to the AI host. It does not weaken Proposal validation, idempotency, or
fail-closed canonical commits.

`setup` without `--install-hooks` still performs setup and returns context, but
it does not create the project binding or hook files. Manual
`shared-mind resume` remains an advanced recovery/custom-budget command. It
discovers the workspace from the project tree, verifies kernel and product
integrity, and returns task-aware `SESSION_READY` context. Request the full
resume ceiling explicitly when deeper evidence is needed:

The global setup skill still recognizes `Shared Mind 초기설정해` as a one-time
natural-language way to run setup from Codex after the package is installed. It
is not a per-session resume step.

```console
$ shared-mind resume --budget-bytes 131072
```

For this repository the existing workspace is `../shared-mind-memory`, so
`shared-mind setup --install-hooks` reuses it without repeating cold start.
Direct `init`, `cold-start`, `session`, and `resume` remain available as
lower-level or advanced surfaces.

The hook-neutral bootstrap surface is available for custom hosts:

```console
$ shared-mind session start
$ shared-mind session prompt --prompt "Review the authentication migration"
```

Both commands use only the current Git root's project binding. Missing,
malformed, moved, or integrity-invalid bindings return
`SESSION_BOOTSTRAP_SKIPPED` with no `additional_context`.

### Automatic and manual observation capture

With `--install-hooks`, Claude Code tool events lazily start an observation
buffer. Session end or stop finalizes the ordered events through the existing
DEV-081 task-capture boundary. Finalization registers immutable source bytes,
runs extraction, and leaves any resulting candidates as reviewable
DraftProposals; valid input may produce zero Drafts. It does not let hook or
model output write canonical memory directly. Identical retries are idempotent.

The same lifecycle is available explicitly for integrations and debugging.
Run from the workspace tree, or place the global
`--workspace <workspace>` option before `observe`:

```console
$ shared-mind-product observe start \
    --session session:example --task DEV-105
$ shared-mind-product observe append \
    --session session:example \
    --event-json '{"object_type":"TASK_TRACE_EVENT","event_version":"task-trace-event@1","event_id":"trace_event_example_1","sequence":1,"event_type":"TASK","occurred_at":"2026-08-21T09:00:00Z","summary":"Begin DEV-105","details":{}}'
$ shared-mind-product observe finalize \
    --session session:example
$ shared-mind-product observe prune \
    --before 2026-09-01T00:00:00Z
```

`append` accepts the versioned task-trace event schema and requires contiguous
sequence numbers plus caller-supplied UTC timestamps. `finalize` requires at
least one valid event. `prune` removes only finalized archives that ended
strictly before the cutoff; pending buffers and canonical/product state are not
changed.

### Live viewer and browser review queue

Launch the local control surface against an explicit workspace:

```console
$ shared-mind-web --workspace ../project-memory --host 127.0.0.1 --port 8126
```

Then open `http://127.0.0.1:8126/observations` for cursor-ordered capture
receipts, canonical event detail, and the relative SSE feed. Open
`http://127.0.0.1:8126/review` to inspect Draft content and provenance and to
commit or reject one selected Draft. The server accepts loopback bindings only.

The review page sends its per-process ephemeral token in the
`X-Shared-Mind-CSRF-Token` header. Direct API clients must do the same for every
POST. Commit delegates to the existing deterministic Proposal/receipt boundary;
reject changes only staged product state. Missing or invalid CSRF, invalid
Drafts, and stale canonical writes fail closed. There is no browser bulk-commit
or automatic approval route.

The equivalent terminal review path is:

```console
$ shared-mind-product review-queue
$ shared-mind-product draft show <draft-id>
$ shared-mind-product draft commit <draft-id>
$ shared-mind-product draft reject <draft-id> \
    --rationale "Evidence is incomplete."
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

### Import and review before committing

```console
$ shared-mind-product ingest ./project --conversation ./sessions.jsonl
$ shared-mind-product extract <batch-id>
$ shared-mind-product draft list --batch-id <batch-id>
$ shared-mind-product draft show <draft-id>
$ shared-mind-product draft commit <draft-id>
$ shared-mind-product draft reject <draft-id> --rationale "Not supported by the source."
$ shared-mind-product build all
```

### Request task-aware context

```console
$ shared-mind resume "Review the authentication migration"
```

Use `shared-mind context` when custom query, reference, depth, token-budget, or
more than the 128 KiB resume safety ceiling is required.

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

See the [Product guide](docs/product-guide.md) for additional product-layer
workflow details.

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
Scenario, retrieval, and observation-finalization determinism are included in
the cross-platform subset.

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
[Apache License 2.0](LICENSE) (`Apache-2.0`). The distribution also carries the
project attribution in [NOTICE](NOTICE). Apache-2.0 permits use, modification,
and distribution subject to its license, notice, attribution, changed-file,
trademark, patent, and other stated terms.
