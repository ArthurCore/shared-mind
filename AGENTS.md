# Shared Mind contributor guide

## Product boundary

Shared Mind preserves sources, assertions, evidence, conflicts, decisions,
questions, work state, shared Skills, and history. It does not claim to
determine truth. LLM output may create reviewable proposals, but every
canonical state transition must remain deterministic and replayable.

> **Project has state. Agents come and go.**

## Invariants

- Keep exactly one canonical Shared State for every Agent, model, and session.
- Never introduce Agent-specific canonical memory tables, profiles, fixed asset
  bindings, or hidden memory forks.
- Task-specific context may differ, but the same state/request/selector/budget
  must produce the same result regardless of the calling client.
- Keep accepted kernel mutations in an append-only ledger.
- Keep rejected attempts in receipts without advancing the ledger head.
- Never overwrite a contradictory active Claim; commit it and open a fact conflict.
- Reject stale destructive operations as transaction conflicts.
- Require verified source bytes for active factual Claims.
- Let extractors create DraftProposals only; never write canonical state directly.
- Treat Scenario, Core Context, Wiki, retrieval, and code indexes as disposable
  projections, never authoritative state.
- Keep Skills shared by identity/version and require passing test evidence before
  approval.
- Pin schema, predicate-registry, conflict-rule, guard-DSL, projection, product,
  builder, selector, and index versions where they affect deterministic output.
- Add or update conformance tests with every semantic change.

## Commands

```bash
python3 contracts/validate_contract.py
python3 contracts/validate_product_contract.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
