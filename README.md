# Shared Mind

Shared Mind is a local-first external memory for carrying sources, evidence,
claims, decisions, questions, conflicts, and work state across AI sessions. A
deterministic SQLite ledger owns canonical changes; Markdown, JSON, search, and
handoff context are reproducible views of that ledger-backed state.

The current local path supports:

- reproducible local workspace initialization;
- immutable, content-hashed Markdown and UTF-8 text source revisions;
- schema-validated, idempotent Proposal commits;
- factual claims with byte-range evidence;
- durable fact conflicts and guarded conflict resolution;
- Decision, OpenQuestion, and WorkItem lifecycle records;
- ledger verification and deterministic replay;
- deterministic Markdown/JSON projection and budgeted handoff context;
- deterministic structured query plus advisory rebase hints;
- a non-interactive JSON CLI with stable result codes; and
- an optional local stdio MCP adapter with the same service envelopes.

Optional, core-outside integrations add three pinned, source-only import
adapters (AtomicStrata, Qarinah, and SwarmVault), an exact-token counter
protocol, and a deny-by-default remote-policy evaluator. These are local
protocols: the repository contains no live vendor connector, credential flow,
or remote write transport.

The product boundary and acceptance criteria are defined in the [SRS](docs/SRS.md).

## Quick start

Shared Mind requires Python 3.11 or newer.

```console
$ python3 -m pip install -e .
$ shared-mind init ./memory --purpose "Preserve this project's reasoning across AI sessions."
$ cd ./memory
$ shared-mind context --budget-tokens 4096
```

Every operational CLI response is one JSON document. A newly initialized
workspace has empty context; add source files beneath its `sources/` directory,
then submit structured Proposals to accumulate canonical state.
`--budget-bytes` is a hard limit. Dependency-free `--budget-tokens` uses the
versioned estimator reported in the context metadata with
`token_estimate_exact: false`; use a
model tokenizer to derive a byte limit when exact model accounting is required.

Coding agents should start with the [Coding-agent bootstrap](docs/agent-bootstrap.md).
It gives the one-command handoff path, the Proposal-only mutation boundary, and
the projection review workflow.

For other integration surfaces, see the [local MCP guide](docs/mcp.md),
[external source adapters](docs/adapters.md), [remote policy boundary](docs/remote-policy.md),
and [product-continuity evaluation](docs/dogfooding.md).

## Authority model

- Source bytes and their hashes are evidence authority.
- The append-only operation ledger is change authority.
- Materialized SQLite tables are replayable current state.
- `projections/project.md`, `projections/project.json`, and context packs are
  non-authoritative views.
- A factual contradiction is preserved as an open `FACT_CONFLICT`; Shared Mind
  does not claim to decide which assertion is true.
- A stale destructive Proposal is rejected as `TRANSACTION_CONFLICT` without
  advancing the ledger; the returned rebase hint is advisory and is never
  auto-applied.

Current writes use schema `1.3.0`. Its canonical `DecisionReceipt` always has
a required, nullable `proposer` field so representable actor provenance is
auditable without fabricating an identity for malformed input. Frozen 1.2
receipts remain byte-preserved, and mixed 1.0-1.3 history remains readable and
replay-verifiable.

Canonical state must change through `proposal commit` or the Proposal-backed
`source add` command. Direct SQLite mutation is outside the public interface.
The in-process SQLite authorizer blocks public DML/DDL; a database file owner
performing external forensic SQL remains outside this local trust boundary.

## Repository layout

```text
contracts/              Versioned JSON Schema, predicate registry, fixtures
docs/                    SRS, agent bootstrap, and verification notes
src/shared_mind/         Kernel, continuity, workspace, CLI, and projections
tests/                   Executable conformance and regression tests
benchmarks/              Opt-in deterministic context benchmark and evidence
evals/                   Offline product-continuity scorer and schemas
AGENTS.md                Contributor invariants
```

## Verify

```bash
python3 contracts/validate_contract.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Current test discovery is **336 standard-library tests**. The earlier
Python 3.13.2 local evidence of 327 tests in 573.553 seconds is superseded by
the live-summary, live-MCP, and canonical-proposition regression tests added
after that run. Contract validation is a separate mandatory gate.

CI is configured for Python 3.11-3.13, Linux/macOS/Windows determinism subsets,
80% branch coverage, lint/type/dependency/security gates, and clean base/MCP
wheel smokes. The current-HEAD hosted full-suite result is still pending; do
not treat this README as a hosted Actions pass claim. Locally, SQLite uses WAL
with `synchronous=FULL`; process-kill and WAL recovery tests cover the durable
commit boundary.

The checked-in [DEV-021 benchmark evidence](benchmarks/results/dev-021-2026-08-11.md)
records completed 100k-entry history-heavy and hot-active fixtures. The final
single-traversal hot-active context returned the byte-identical canonical
output at p95 1.653288 seconds, under the 2-second NFR-008 target; the
history-heavy p95 was 2.707 milliseconds. The approximately 17% hot-active
margin is environment-sensitive and remains a regression watch point. On the
frozen `47b7f1c` implementation, clean 100k verification completed in 476.764
seconds and explicit replay in 255.182 seconds with exact receipt count, head,
and state-root parity. These were persisted schema-1.2 fixtures verified and
replayed by schema-1.3 code, not freshly generated schema-1.3 fixtures. Earlier
contaminated timings remain in the raw artifact but are not used as performance
claims.

The CLI can also verify that ledger hashes and replayed state agree:

```console
$ shared-mind replay --verify
```
