# DEV-103 Live Observation Stream TDD evidence

## Source plan and scope

The source plan is
[`../DEV-102-104-auto-observation-capture-plan.md`](../DEV-102-104-auto-observation-capture-plan.md),
section 5. Only DEV-103 was implemented after an independent DEV-102 gate. DEV-104
was not started.

The existing DEV-071 surface established the loopback-only bind check and
ProductService delegation boundary. DEV-103 keeps both. Product audit sequence was
selected as the received-order cursor because it already orders accepted capture
receipts without adding storage. SSE is a finite cursor poll that browser EventSource
reconnects, avoiding a new background queue or long-lived write-capable component.

## RED and focused GREEN

| Stage | Command | Actual result |
|---|---|---|
| RED | `PYTHONPATH=src python3 -m unittest -v tests.test_web_observations` | 6 tests ran. Existing non-loopback rejection passed; four absent routes produced 7 intended 404 assertions and 1 downstream missing-data error. No syntax, fixture, dependency, or setup failure. |
| GREEN | `PYTHONPATH=src python3 -m unittest -v tests.test_web_observations` | 6/6 PASS. |

RED checkpoint: `f5912f3` (`test: add RED acceptance suite for DEV-103 observation stream`).

## Acceptance specification

| # | What is guaranteed | Test | Result |
|---|---|---|---|
| 1 | List exposes the capture receipt and detail exactly matches canonical trace/event source bytes | `WebObservationTest.test_list_and_detail_restore_exact_canonical_trace_events` | PASS |
| 2 | A capture accepted after the baseline cursor appears as a valid SSE receipt event | `WebObservationTest.test_sse_stream_emits_new_capture_receipt_after_cursor` | PASS |
| 3 | Exclusive cursor pagination follows received order without duplicates or omissions | `WebObservationTest.test_cursor_pagination_is_received_order_without_duplicate_or_omission` | PASS |
| 4 | Non-loopback server binding remains rejected | `WebObservationTest.test_non_loopback_server_binding_remains_rejected` | PASS |
| 5 | List, detail, stream, and HTML routes open no ProductStore write transaction and change no kernel/audit state | `WebObservationTest.test_all_observation_routes_open_no_product_write_transaction` | PASS |
| UI | `/observations` is dependency-free, uses relative SSE, and carries the polling cursor | `WebObservationTest.test_observations_html_is_dependency_free_and_cursor_aware` | PASS |

## Final required gates

| Gate | Actual result |
|---|---|
| `python3 contracts/validate_contract.py` | PASS: 7 predicates, 16 typed fixtures, 6 negative cases, 6 semantic cases, and 7 continuity operations. |
| `python3 contracts/validate_product_contract.py` | PASS: 10 typed fixtures and 14 negative cases. |
| `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 545 tests, 0 failures/errors; 1 pre-existing optional MCP SDK v1 skip. |

## Coverage and exclusions

The repository's required complete discovery gate is recorded above; the DEV-103 plan
does not require a separate coverage command. Tests exercise all four new routes,
cursor/SSE behavior, canonical source reconstruction, loopback preservation, and the
no-write boundary. DEV-104 draft commit/reject routes, CSRF state, and review queue UI
are explicitly excluded.
