# DEV-082~086 — Shared-state continuity evaluations

> **Project has state. Agents come and go.**

This milestone evaluates one canonical Shared State.  Evaluation scenarios,
responses, lifecycle summaries, and benchmark reports are disposable evidence;
they never become an Agent-specific memory partition or a second source of
truth.

## Common boundary

- Evaluators are deterministic, provider-neutral, and read-only.
- Expected IDs and truths must point at canonical records or immutable source
  revisions in the evaluated workspace.
- Scenario/Core/context documents are inputs to an evaluation, not authority.
- Project mutations still require a kernel `Proposal`; Skill mutations still
  require a `ProductMutationProposal`.
- Every report pins the context hash, kernel state root, evaluator version, and
  observed response hash.

## DEV-082 — Zero-Relearning Evaluation

A fresh session receives only a task-aware context and returns a strict
observation.  The grader measures:

- Continuity Accuracy
- Decision Recall
- Open Question Recall
- Conflict Recall
- Evidence Traceability
- Wrong Memory Rate
- Missing Critical Memory Rate
- Irrelevant Context Rate
- Context bytes/tokens
- Time To Productive Action

The self-dogfood scenario requires exact recovery of project purpose, One
Shared State, the reason Agent Loadout was removed, Core Context's non-authority,
the two mutation boundaries, the current active WorkItem, and source evidence.

## DEV-083 — Memory Pollution / Wrong Memory Evaluation

The pollution grader distinguishes duplicate semantic memories, irrelevant
memories, stale leakage, wrong values, and confidently wrong values.  A wrong
memory is never converted into canonical state by the grader.

## DEV-084 — Memory Lifecycle

Canonical statuses are normalized into `CURRENT`, `STALE`, `SUPERSEDED`, or
`COMPLETED`.  Non-current records remain historical and evidence-preserving,
but only current records are eligible for current-state answers.

## DEV-085 — Conflict Resolution Workflow

The workflow evaluator accepts canonical before/after conflict snapshots and
requires the same conflict ID, episode, member digest, and original Claim
documents.  A valid resolution partitions every member into selected/rejected
sets and retains the resolver, rationale, evidence links, and decision time.

## DEV-086 — Context Quality Benchmark

The benchmark combines relevant recall, missing-critical rate, irrelevant
context rate, evidence traceability, exact UTF-8 bytes, counted tokens, and
time-to-productive-action.  Thresholds are scenario data and are reported
without silently changing the measured result.

## Acceptance sequence

For each DEV: define the contract, record a failing test, implement the minimal
deterministic behavior, run targeted tests, run the full branch-coverage suite,
exercise the real `../shared-mind-memory` workspace, capture the task trace,
consolidate/verify, then update the matching WorkItem through a kernel Proposal.

