# Shared Mind

Shared Mind is a local-first external memory for carrying sources, evidence,
claims, decisions, questions, conflicts, and work state across AI sessions. A
deterministic SQLite ledger owns canonical changes; Markdown, JSON, search, and
handoff context are reproducible views of that ledger-backed state.

The current P0 path supports:

- reproducible local workspace initialization;
- immutable, content-hashed Markdown and UTF-8 text source revisions;
- schema-validated, idempotent Proposal commits;
- factual claims with byte-range evidence;
- durable fact conflicts and guarded conflict resolution;
- Decision, OpenQuestion, and WorkItem lifecycle records;
- ledger verification and deterministic replay;
- deterministic Markdown/JSON projection and budgeted handoff context; and
- a non-interactive JSON CLI with stable result codes.

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

## Authority model

- Source bytes and their hashes are evidence authority.
- The append-only operation ledger is change authority.
- Materialized SQLite tables are replayable current state.
- `projections/project.md`, `projections/project.json`, and context packs are
  non-authoritative views.
- A factual contradiction is preserved as an open `FACT_CONFLICT`; Shared Mind
  does not claim to decide which assertion is true.
- A stale destructive Proposal is rejected as `TRANSACTION_CONFLICT` without
  advancing the ledger.

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
AGENTS.md                Contributor invariants
```

## Verify

```bash
python3 contracts/validate_contract.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The CLI can also verify that ledger hashes and replayed state agree:

```console
$ shared-mind replay --verify
```
