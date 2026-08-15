# DEV-087 — Paired Context Reduction Evaluation

> **Project has state. Agents come and go.**

DEV-087 answers the last open DEV-086 question with a same-state paired
measurement.  A full baseline context and a compact candidate context are
evaluated against the same explicit expectation.  Neither context, observation,
nor report becomes canonical truth.

## Contract

`evaluate_paired_context_reduction` accepts:

- baseline context and fresh-session observation;
- candidate context and fresh-session observation;
- one zero-relearning expectation;
- explicit, versioned reduction thresholds; and
- measured elapsed milliseconds and token counts for both arms.

Both context hashes are recomputed and both contexts must pin the same kernel
state root.  Each arm is scored by the existing zero-relearning evaluator.  The
paired report records exact, unclamped reductions for UTF-8 bytes, counted
tokens, and time-to-productive-action.  It passes only when:

1. both zero-relearning quality gates pass;
2. continuity, decision/question/conflict recall, and evidence traceability do
   not regress;
3. wrong, missing-critical, and irrelevant-memory rates do not increase; and
4. every declared reduction threshold passes.

The stable failure surface includes state-root mismatch, malformed thresholds,
invalid measurements, quality regression, non-reduction, and per-resource
threshold failure.  A negative reduction remains negative in the report; the
evaluator never clamps or relabels it as a saving.

## Interfaces and evidence

- Python: `ProductService.evaluate_paired_context_reduction`
- CLI: `shared-mind-product metrics context-reduction ...`
- MCP: `continuity_evaluate` with `PAIRED_CONTEXT_REDUCTION`
- immutable runner: `run_paired_evaluation`
- report schema: `paired-context-reduction-eval@1`

The checked-in DEV-087 fixtures were built from the real sibling workspace
`../shared-mind-memory`.  The baseline uses `EVIDENCE` depth and a 65,536-byte
budget; the candidate uses `SUMMARY` depth and a 24,576-byte budget.  The task,
query, explicit references, selector version, and canonical state are otherwise
shared.  Raw latency samples, the median, byte counts, and deterministic
`ceil(UTF-8 bytes / 4)` token estimates are retained alongside the report.
Full context bodies are evaluated from a private temporary directory because
evidence locators may contain workstation-local absolute paths; checked-in
evidence retains their hashes and safe aggregate metrics instead.

## Authority boundary

- Evaluation is read-only with respect to canonical Shared State.
- Factual/project status changes still use a validated kernel Proposal.
- Skill changes still use a `ProductMutationProposal`.
- Scenario, Core Context, selection traces, and evaluation reports remain
  disposable projections/evidence.
- No Agent-, model-, or client-specific canonical memory is introduced.
