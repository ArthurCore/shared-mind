# Shared Mind

Shared Mind is a local-first external memory that lets a new AI session continue
from a user's sources, evidence, decisions, questions, and work state. Its
deterministic epistemic transaction kernel is the current implementation layer;
the continuity records, projections, and handoff interface remain future work.

This repository contains the first Atlas vertical slice:

- SQLite WAL-backed append-only mutation ledger
- separate receipts for accepted and rejected commit attempts
- idempotent `commit(proposal)`
- Draft 2020-12 runtime validation for sources and proposals
- exact schema, registry, conflict-rule, guard-DSL, and projection version checks
- deterministic predicate and evidence validation
- exclusive-value fact conflict creation
- kernel-required Claim reads for destructive supersede operations
- stale aggregate detection as a transaction conflict
- structured normalization of malformed input and SQLite integrity errors
- conflict-aware reads that return every active claim and open conflict

## Layout

```text
contracts/              Atlas Predicate Registry v1 and JSON Schema
src/shared_mind/         kernel implementation
tests/                   executable vertical-slice conformance tests
AGENTS.md                invariants for coding agents
```

## Verify

```bash
python3 contracts/validate_contract.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Current boundary

The implementation supports the operations required by the first vertical
slice: `ASSERT_CLAIM`, `ATTACH_EVIDENCE`, and `SUPERSEDE_CLAIM`. The contract
also defines ledger-backed source registration, retraction, and conflict
resolution. Those operations, deterministic replay/projection, continuity
records, and the CLI are the next implementation slices.
