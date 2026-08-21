"""Dependency-free DEV-104 review queue HTML rendering."""

from __future__ import annotations

import json


def render_review_html(csrf_token: str) -> str:
    token = json.dumps(csrf_token)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shared Mind Review Queue</title><style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#18181b}}
h1{{margin-bottom:.25rem}}small{{color:#52525b}}.grid{{display:grid;grid-template-columns:1fr 2fr;gap:1rem}}
button,select{{padding:.55rem;margin:.25rem}}#drafts button{{display:block;width:100%;text-align:left}}
pre{{white-space:pre-wrap;background:#f4f4f5;padding:1rem;border-radius:.5rem;overflow:auto}}
@media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}</style></head>
<body><h1>Shared Mind Review Queue</h1><small>Review provenance before canonical promotion.</small>
<p><label>State <select id="state"><option>DRAFT</option><option>REVIEWED</option><option>FAILED</option><option>COMMITTED</option><option>REJECTED</option></select></label>
<button id="refresh">Refresh</button></p><div class="grid"><section><h2>Drafts</h2><div id="drafts"></div></section>
<section><h2>Selected draft and provenance</h2><pre id="detail">Select one draft</pre>
<button id="commit" disabled>Commit selected</button><button id="reject" disabled>Reject selected</button></section></div>
<script>
const csrf={token},csrfHeader='X-Shared-Mind-CSRF-Token',commitSuffix='/commit',rejectSuffix='/reject';
const drafts=document.getElementById('drafts'),detail=document.getElementById('detail'),state=document.getElementById('state');let selected=null;
async function show(id){{const r=await fetch('/api/drafts/'+encodeURIComponent(id));const d=(await r.json()).data;selected=d;detail.textContent=JSON.stringify(d,null,2);document.getElementById('commit').disabled=false;document.getElementById('reject').disabled=false}}
async function load(){{const r=await fetch('/api/drafts?state='+encodeURIComponent(state.value));const d=(await r.json()).data;drafts.textContent='';d.drafts.forEach(item=>{{const b=document.createElement('button');b.textContent=item.draft_id+' · '+item.draft_kind;b.onclick=()=>show(item.draft_id);drafts.appendChild(b)}})}}
async function mutate(suffix,rationale){{if(!selected)return;const body={{draft_id:selected.draft_id}};if(rationale!==undefined)body.rationale=rationale;const r=await fetch('/api/drafts/'+encodeURIComponent(selected.draft_id)+suffix,{{method:'POST',headers:{{'Content-Type':'application/json',[csrfHeader]:csrf}},body:JSON.stringify(body)}});detail.textContent=JSON.stringify(await r.json(),null,2);await load()}}
document.getElementById('refresh').onclick=load;state.onchange=load;document.getElementById('commit').onclick=()=>mutate(commitSuffix);document.getElementById('reject').onclick=()=>{{const rationale=prompt('Rejection rationale');if(rationale)mutate(rejectSuffix,rationale)}};load();
</script></body></html>"""


__all__ = ["render_review_html"]
