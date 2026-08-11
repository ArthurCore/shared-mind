# Shared Mind contributor guide

## Product boundary

Shared Mind preserves assertions, evidence, conflicts, decisions, and history.
It does not claim to determine truth. LLM output may create proposals, but every
canonical state transition must remain deterministic and replayable.

## Invariants

- Keep accepted mutations in an append-only ledger.
- Keep rejected attempts in receipts without advancing the ledger head.
- Never overwrite a contradictory active claim; commit it and open a fact conflict.
- Reject stale destructive operations as transaction conflicts.
- Pin schema, predicate-registry, conflict-rule, guard-DSL, and projection versions.
- Treat Markdown and search indexes as projections, never authoritative state.
- Add or update conformance tests with every semantic change.

## Commands

```bash
python3 contracts/validate_contract.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

