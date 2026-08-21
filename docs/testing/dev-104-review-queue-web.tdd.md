# DEV-104 Review Queue Web TDD evidence

## Source plan and scope

The source plan is
[`../DEV-102-104-auto-observation-capture-plan.md`](../DEV-102-104-auto-observation-capture-plan.md),
section 6. DEV-104 began only after DEV-103 passed independent focused and full gates.

Existing inspection confirmed that ProductService already owns Draft list/get,
validation, commit, reject, idempotency, stored receipt, and audit behavior. The Web
implementation therefore adds only browser selection/rendering and request security;
it does not introduce a new mutation service or storage object.

## RED and focused GREEN

| Stage | Command | Actual result |
|---|---|---|
| RED | `PYTHONPATH=src python3 -m unittest -v tests.test_web_review_queue tests.test_product_interfaces` | 13 tests ran: 5 existing non-Web/loopback paths passed; missing review page/token/header interface produced 6 intended failures and 2 intended errors. Draft fixtures and dependencies were valid. |
| GREEN | `PYTHONPATH=src python3 -m unittest -v tests.test_web_review_queue tests.test_product_interfaces` | 13/13 PASS. |
| Compatibility RED | `PYTHONPATH=src python3 -m unittest -v tests.test_web_review_queue.WebReviewQueueTest.test_existing_edit_body_remains_compatible_without_redundant_draft_id` | 1/1 intended failure: historical edit body was rejected as `DRAFT_ID_MISMATCH`. |
| Compatibility GREEN | Same single-test command | 1/1 PASS. Final DEV-104/interface focused suite: 14/14 PASS. |

RED checkpoint: `4db961a` (`test: add RED acceptance suite for DEV-104 review queue`).
Compatibility RED checkpoint: `2b2cd9a` (`test: add RED regression for DEV-104 edit compatibility`).

## Acceptance specification

| # | What is guaranteed | Test | Result |
|---|---|---|---|
| 1 | Web commit calls the same ProductService boundary and CLI re-commit returns the identical stored receipt/audit outcome | `WebReviewQueueTest.test_web_commit_and_cli_commit_return_identical_receipt_and_audit_outcome` | PASS |
| 2 | Reject leaves kernel ledger head/hash/count and state root unchanged | `WebReviewQueueTest.test_web_reject_changes_no_kernel_ledger_head_or_state_root` | PASS |
| 3 | Web re-commit is idempotent with no new kernel or product-audit history | `WebReviewQueueTest.test_web_recommit_is_idempotent_without_new_ledger_or_product_audit` | PASS |
| 4 | Invalid Draft commit fails closed with zero canonical mutation | `WebReviewQueueTest.test_validation_failure_is_fail_closed_with_zero_canonical_mutation` | PASS |
| 5 | Per-application tokens differ; missing/wrong CSRF fails before service mutation; non-loopback remains rejected | `WebReviewQueueTest.test_state_changing_posts_require_ephemeral_csrf_before_service_mutation` | PASS |
| UI | `state` list, detail provenance, inline token, explicit commit/reject, and absence of bulk/automatic approval | `WebReviewQueueTest.test_draft_state_detail_provenance_and_review_page_have_no_bulk_path` | PASS |
| Compatibility | Existing edit with `{document, expected_version}` remains valid while commit without explicit `draft_id` remains rejected | `WebReviewQueueTest.test_existing_edit_body_remains_compatible_without_redundant_draft_id` | PASS |

## Final required gates

| Gate | Actual result |
|---|---|
| `python3 contracts/validate_contract.py` | PASS: 7 predicates, 16 typed fixtures, 6 negative cases, 6 semantic cases, and 7 continuity operations. |
| `python3 contracts/validate_product_contract.py` | PASS: 10 typed fixtures and 14 negative cases. |
| `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 552 tests, 0 failures/errors; 1 pre-existing optional MCP SDK v1 skip. |

## Coverage and exclusions

The required full discovery gate is recorded above; the plan does not require a
separate coverage command. Focused tests exercise all five promotion guarantees,
every existing Web POST category's CSRF precondition, provenance visibility, and the
dependency-free page. Bulk approval, automatic promotion, remote binding, CORS, and a
new Draft mutation/storage boundary are explicitly excluded.
