# Shared Mind contributor guide

## Current dogfooding entrypoint

For DEV-080 and later self-dogfooding work, read these files before changing code:

1. [`docs/DEV-080-self-dogfooding.md`](docs/DEV-080-self-dogfooding.md)
2. [`docs/self-dogfooding-bootstrap.md`](docs/self-dogfooding-bootstrap.md)
3. [`ROADMAP.md`](ROADMAP.md)

A fresh Codex/Claude/GPT session should recover project state from the local Shared Mind workspace instead of relying on a long explanatory prompt from the user. The recommended local workspace is `../shared-mind-memory`, outside this Git repository.

The first self-dogfooding invariant is:

> **Use Shared Mind to understand and continue Shared Mind.**

After a real development task, capture the task trace back into the same Shared State before ending the session. Never create client-specific project memories.

For an installed checkout, `shared-mind resume` is the default session entrypoint:
it discovers the sibling `*-memory` workspace, verifies integrity, and returns
task-aware context in one command. Use the longer context command only for
custom selectors or budgets.

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
