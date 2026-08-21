"""Read-only observation receipt and canonical trace projections for DEV-103."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .canonical import canonical_json
from .product import ProductError, ProductService


DEFAULT_OBSERVATION_LIMIT = 20
MAX_OBSERVATION_LIMIT = 100


OBSERVATIONS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shared Mind Observations</title><style>
body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#18181b}
h1{margin-bottom:.25rem}small{color:#52525b}.grid{display:grid;grid-template-columns:1fr 2fr;gap:1rem}
button{display:block;width:100%;text-align:left;padding:.6rem;margin:.25rem 0}pre{white-space:pre-wrap;background:#f4f4f5;padding:1rem;border-radius:.5rem;overflow:auto}
@media(max-width:800px){.grid{grid-template-columns:1fr}}</style></head>
<body><h1>Shared Mind Observations</h1><small>Raw evidence stream; canonical promotion still requires review.</small>
<div class="grid"><section><h2>Captured traces</h2><div id="list">Loading</div></section>
<section><h2>Trace detail</h2><pre id="detail">Select a trace</pre></section></div>
<script>
let cursor=0;const list=document.getElementById('list'),detail=document.getElementById('detail');
function add(item){if(document.getElementById('obs-'+item.cursor))return;const b=document.createElement('button');b.id='obs-'+item.cursor;b.textContent=item.receipt.trace_id+' · '+item.receipt.event_count+' events';b.onclick=async()=>{const r=await fetch('/api/observations/'+encodeURIComponent(item.receipt.trace_id));detail.textContent=JSON.stringify((await r.json()).data,null,2)};list.appendChild(b);cursor=Math.max(cursor,item.cursor)}
async function poll(){const r=await fetch('/api/observations?limit=100&after='+cursor);const d=(await r.json()).data;d.observations.forEach(add)}
function stream(){const source=new EventSource('/api/observations/stream?after='+cursor);source.addEventListener('observation',e=>add(JSON.parse(e.data)));source.onerror=()=>{source.close();poll().finally(()=>setTimeout(stream,1000))}}
list.textContent='';poll().then(stream);
</script></body></html>"""


class ObservationReadModel:
    """Project capture receipts and immutable source bytes without mutations."""

    def __init__(self, service: ProductService):
        self.service = service

    def list(self, *, limit: str | None = None, after: str | None = None) -> dict[str, Any]:
        page_limit = _integer_parameter(
            limit,
            name="limit",
            default=DEFAULT_OBSERVATION_LIMIT,
            minimum=1,
            maximum=MAX_OBSERVATION_LIMIT,
        )
        after_cursor = _integer_parameter(
            after, name="after", default=0, minimum=0, maximum=None
        )
        records = self.service.list_task_capture_records(
            after_cursor=after_cursor, limit=page_limit + 1
        )
        has_more = len(records) > page_limit
        observations = records[:page_limit]
        next_cursor = (
            int(observations[-1]["cursor"]) if observations else after_cursor
        )
        return {
            "observations": observations,
            "count": len(observations),
            "after": after_cursor,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def detail(self, trace_id: str) -> dict[str, Any]:
        record = self.service.get_task_capture_record(trace_id)
        if record is None:
            raise ProductError("OBSERVATION_NOT_FOUND", trace_id)
        receipt = record["receipt"]
        span = self.service.read_source_span_projection(
            str(receipt["source_revision_id"])
        )
        if span["excerpt_hash"] != receipt["content_hash"]:
            raise ProductError(
                "OBSERVATION_SOURCE_MISMATCH",
                "Capture receipt content hash does not match canonical source bytes.",
            )
        try:
            trace = json.loads(span["excerpt"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProductError(
                "OBSERVATION_SOURCE_INVALID",
                "Canonical observation source is not a task trace.",
            ) from exc
        if (
            not isinstance(trace, Mapping)
            or trace.get("trace_id") != trace_id
            or not isinstance(trace.get("events"), list)
        ):
            raise ProductError(
                "OBSERVATION_SOURCE_MISMATCH",
                "Canonical task trace does not match the capture receipt.",
            )
        normalized = dict(trace)
        return {
            "cursor": record["cursor"],
            "receipt": receipt,
            "trace": normalized,
            "events": normalized["events"],
        }

    def stream(self, *, after: str | None = None) -> bytes:
        after_cursor = _integer_parameter(
            after, name="after", default=0, minimum=0, maximum=None
        )
        records = self.service.list_task_capture_records(
            after_cursor=after_cursor, limit=MAX_OBSERVATION_LIMIT
        )
        lines = ["retry: 1000", ""]
        for record in records:
            lines.extend(
                (
                    f"id: {record['cursor']}",
                    "event: observation",
                    f"data: {canonical_json(record)}",
                    "",
                )
            )
        if not records:
            lines.extend((": no observations", ""))
        return ("\n".join(lines) + "\n").encode("utf-8")


def _integer_parameter(
    value: str | None,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int | None,
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProductError(
            "OBSERVATION_PAGE_INVALID", f"{name} must be an integer"
        ) from exc
    if parsed < minimum or maximum is not None and parsed > maximum:
        boundary = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        raise ProductError(
            "OBSERVATION_PAGE_INVALID", f"{name} must be within {boundary}"
        )
    return parsed


__all__ = [
    "DEFAULT_OBSERVATION_LIMIT",
    "MAX_OBSERVATION_LIMIT",
    "OBSERVATIONS_HTML",
    "ObservationReadModel",
]
