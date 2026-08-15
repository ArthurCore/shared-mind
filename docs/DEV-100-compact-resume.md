# DEV-100 — Compact Resume Context

## Problem

DEV-099 made `shared-mind resume` the correct one-command cold-start path, but
its 128 KiB default also became a packing target. The real Shared Mind
workspace returned 130,997 of 131,072 bytes even though a new coding session
only needs the continuity core and references for later evidence drill-down.

## User journey

A normal session starts compactly:

```console
$ shared-mind resume
```

An evidence-heavy task opts into the old full allowance explicitly:

```console
$ shared-mind resume --budget-bytes 131072
```

Requests above the resume ceiling use the advanced `shared-mind context`
surface, where task, query, references, depth, byte budget, and token budget
remain independently configurable.

## Contract

1. Default `resume` keeps EVIDENCE depth and uses a 24,576-byte hard budget.
2. Resume accepts an explicit budget up to and including 131,072 bytes and
   rejects a larger value at the CLI boundary.
3. Product/kernel integrity is verified before context construction; invalid
   integrity still fails closed.
4. The compact core contains project purpose, all active decisions, all open
   questions, every open conflict with its member Claims, and every actionable
   WorkItem. It never hides a mandatory continuity record to meet the budget.
5. Core and task records retain deterministic `projection_ref`, source revision,
   evidence, and related-object pointers appropriate to their depth. Full
   history and source bytes stay available through on-demand drill-down.
6. The same canonical state, request, selector version, and budget produce the
   same context and context hash regardless of Agent or model.
7. Context remains a disposable projection. DEV-100 adds no Agent profile,
   memory partition, direct database mutation, or new canonical state.

## Acceptance

- The parser and end-to-end CLI expose the 24 KiB default.
- A seeded workspace restores purpose, decision, question, conflict, and work
  state twice with byte-for-byte identical context.
- Final canonical JSON bytes stay within 24,576 and reported core token
  estimates equal `ceil(rendered UTF-8 bytes / 4)`.
- `--budget-bytes 131072` is accepted and 131,073 is rejected.
- The real sibling workspace demonstrates a material bytes/tokens reduction
  without losing the active P0 WorkItem or its evidence pointers.
- Contract validators, full regression, coverage, quality, security, product
  verification, and ledger replay pass before closeout.
