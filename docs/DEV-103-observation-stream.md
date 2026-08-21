# DEV-103 — Live Observation Stream in Web Control

> **Project has state. Agents come and go.**
>
> **관찰은 자동, 정본 승격은 검문.**

상태: **DONE (local gates)**

## Read-only HTTP contract

The existing loopback-only `shared-mind-web` application exposes:

```text
GET /api/observations?limit=&after=
GET /api/observations/<trace-id>
GET /api/observations/stream?after=
GET /observations
```

`/api/observations` returns capture receipts in received order. The cursor is the
integer sequence of the existing `TASK_TRACE_CAPTURED` product-audit event; `after`
is exclusive. The default limit is 20 and the accepted range is 1 through 100.
Each page returns `observations`, `count`, `after`, `next_cursor`, and `has_more`, so
following `next_cursor` visits every receipt without duplication or omission even
when unrelated product-audit events occur between captures.

`/api/observations/<trace-id>` resolves the existing receipt, reads its immutable
kernel `SourceRevision` through a telemetry-free ProductService read facade, verifies
the source-byte hash against the receipt, and returns the receipt, complete trace, and
event list. It does not use a captured-buffer copy as authority.

`/api/observations/stream` is a finite Server-Sent Events polling response. It emits
up to 100 receipts after the exclusive cursor as `event: observation` with audit
sequence `id` values and canonical JSON `data`. An empty response remains valid SSE.
The viewer reconnects with its latest cursor and uses the JSON list endpoint as a
polling fallback; the HTTP worker is not held indefinitely.

`/observations` is one dependency-free HTML document. Its inline JavaScript uses only
browser `fetch`, `EventSource`, DOM APIs, and relative loopback routes. It loads the
received-order list, follows live receipt events, and fetches exact detail on demand.

## Authority and mutation boundaries

- Receipt order and metadata come only from the existing append-only product audit.
- Trace/event bytes come only from the canonical kernel `SourceRevision` referenced by
  the receipt.
- ProductStore exposes SELECT-only capture-record methods; WebControlApplication never
  accesses `store.connection` or SQLite directly.
- New routes open no ProductStore write transaction and add no table, schema, audit,
  telemetry, canonical source, ledger entry, or receipt.
- The existing `_require_loopback` bind check is unchanged. This change adds no remote
  listener or non-loopback exception.
- Observation display does not promote raw evidence. Claim, Decision, Question,
  WorkItem, and Skill changes retain their existing review and Proposal boundaries.
- No Agent-, model-, role-, or session-specific canonical memory is introduced.

## Error contract

- Unknown trace IDs return `OBSERVATION_NOT_FOUND`.
- Invalid cursor or limit input returns `OBSERVATION_PAGE_INVALID`.
- A receipt/source byte mismatch fails closed as `OBSERVATION_SOURCE_MISMATCH`.
- Malformed canonical task-trace bytes fail as `OBSERVATION_SOURCE_INVALID`.

Acceptance and exact RED/GREEN evidence are recorded in
[`testing/dev-103-observation-stream.tdd.md`](testing/dev-103-observation-stream.tdd.md).
