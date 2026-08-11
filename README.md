# Shared Mind Kernel

Shared Mind is a deterministic epistemic transaction kernel for multiple agents
sharing external memory. It preserves claims, their source evidence, factual
conflicts, decisions, and mutation history without silently overwriting a
different assertion.

This repository contains the first Atlas vertical slice:

- SQLite WAL-backed append-only mutation ledger
- separate receipts for accepted and rejected commit attempts
- idempotent `commit(proposal)`
- deterministic predicate and evidence validation
- exclusive-value fact conflict creation
- stale aggregate detection as a transaction conflict
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
also defines source registration, retraction, and conflict resolution; those
remaining operations are the next implementation slice.
