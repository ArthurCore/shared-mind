# DEV-080 self-dogfooding TDD evidence

## User journeys

1. A fresh session can cold-start Shared Mind from the repository and recover
   only the intentional bootstrap Decisions, Questions, WorkItems, and Skill.
2. Directive examples in Markdown or conversations never become canonical
   project state.
3. Code remains an immutable, searchable source without being interpreted as
   deterministic project directives.
4. Incremental consolidation remains verifiable when an unrelated canonical
   mutation leaves a local Scenario's member dependencies unchanged.

## RED and GREEN evidence

| Stage | Command | Result |
|---|---|---|
| Directive RED | Four targeted `tests.test_product_ingest.ProductIngestTest` methods | 4 failures: extractor stayed at `@1`, fenced Markdown produced 4 operations, fenced conversation produced 2 operations, and code produced a draft. |
| Directive GREEN | `PYTHONPATH=src python3 -m unittest -v tests.test_product_ingest` | 12/12 passed. |
| Scenario RED | `PYTHONPATH=src python3 -m unittest -v tests.test_memory_views_product.MemoryViewsProductTest.test_incremental_consolidation_changes_only_after_state_change` | Failed with `artifact_scenario-subject-*` in `derived_views.mismatched`. |
| Scenario GREEN | `PYTHONPATH=src python3 -m unittest -v tests.test_memory_views_product tests.test_product_governance_eval` | 16/16 passed. |

## Cold-start acceptance

The active canonical workspace is outside the repository at
`../shared-mind-memory`. The polluted first attempt was exported and moved out
of the active path before the clean run.

```text
sources             141
decisions             7
open questions        4
work items             7
shared skills          1
claims                 0
open conflicts         0
extraction drafts      2 (one kernel proposal, one Skill)
committed drafts       2
```

The clean run used `deterministic-directives@2`. `shared-mind-product verify`
reported a valid kernel ledger, product audit chain, Skill replay, and derived
view rebuild. DEV-080 was then moved from `TODO` version 1 to `DONE` version 2
through `proposal_complete_dev_080_self_dogfooding_001` at ledger sequence 143.
The session trace was captured as an immutable conversation source.

## Final verification

All commands ran under Python 3.13.2.

| Gate | Result |
|---|---|
| Kernel contract validator | PASS: 7 predicates, 16 typed, 6 negative, 6 semantic, 7 continuity operations |
| Product contract validator | PASS: 8 typed, 11 negative |
| Focused cold-start/ingest/bootstrap | 26/26 PASS |
| Compile + shipped-source Ruff | PASS |
| Configured mypy surface | PASS |
| Bandit medium/high scan | PASS |
| Full parallel branch coverage | 391 tests across 48 files, 0 failures, 82% total coverage |
| Active dogfood workspace verify | `PRODUCT_INTEGRITY_VALID` |

## Checkpoint commits

- `c6dbc77` — directive pollution RED
- `a9ae138` — directive pollution GREEN
- `748e052` — incremental Scenario verification RED
- `e768898` — incremental Scenario verification GREEN

