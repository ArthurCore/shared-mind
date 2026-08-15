# Shared Mind Product Guide

This guide covers the product workflow introduced in package `0.3.0`. Kernel
Proposal, conflict, replay, and projection commands remain documented in
[`docs/agent-bootstrap.md`](agent-bootstrap.md) and [`docs/mcp.md`](mcp.md).

## 1. Initialize

```bash
python3 -m pip install -e .
shared-mind init ./memory --purpose "Preserve project reasoning and work state."
cd ./memory
```

Every CLI command emits exactly one JSON document on stdout.

## 2. Cold start

For a first import, use the integrated flow:

```bash
shared-mind-product cold-start ./project \
  --conversation ./sessions.jsonl \
  --task "Continue implementation" \
  --budget-bytes 65536
```

The report distinguishes imported, unchanged, failed, drafted, committed,
stale, and conflicting records. Re-running unchanged input does not create new
SourceRevisions or duplicate memory candidates.

Deterministic directives supported by the local extractor include:

```text
FACT: <subject> | <predicate> | <object> | <scope>
DECISION: <title> | <conclusion> | <rationale>
QUESTION: <question> | <context>
WORK: <P0..P3> | <description>
SKILL: <purpose> | <trigger> | <step>
```

The original input bytes remain the evidence authority. Directives are a local,
provider-free bootstrap format, not a requirement for future model adapters.

## 3. Manual review flow

```bash
shared-mind-product ingest ./project --conversation ./sessions.jsonl
shared-mind-product extract <batch-id>
shared-mind-product draft list --batch-id <batch-id>
shared-mind-product draft show <draft-id>
```

A reviewer may edit or reject a candidate:

```bash
shared-mind-product draft edit <draft-id> ./replacement.json \
  --expected-version 1
shared-mind-product draft reject <draft-id> \
  --rationale "The source does not support this conclusion."
```

Commit one or all deterministic candidates:

```bash
shared-mind-product draft commit <draft-id>
shared-mind-product draft commit-batch <batch-id>
```

Model-backed candidates are skipped by the default batch commit. They require
explicit inclusion after review.

## 4. Build disposable views

```bash
shared-mind-product build views
shared-mind-product build indexes
shared-mind-product build all
```

Views and indexes may be deleted and rebuilt. They are not canonical truth.
`consolidate` rebuilds only dependencies whose digest changed:

```bash
shared-mind-product consolidate
```

## 5. Task-aware context

```bash
shared-mind-product context \
  --task "Review release readiness" \
  --purpose "Ship version 9 safely" \
  --query "blocking incidents and unresolved decisions" \
  --ref decision_example \
  --depth EVIDENCE \
  --budget-bytes 32768
```

The kernel CLI provides the same context path:

```bash
shared-mind context --task "Review release readiness" --budget-bytes 32768
```

Do not pass model or Agent identity as a memory scope. The selector accepts one
Shared State and changes only the task view.

## 6. Search and on-demand tools

```bash
shared-mind-product search "database migration" --limit 20
shared-mind-product tool capabilities
shared-mind-product tool get_artifact \
  --arguments '{"artifact_id":"artifact_scenario-project"}'
shared-mind-product tool read_source_span \
  --arguments '{"revision_id":"revision_...","start_byte":0,"end_byte":400}'
shared-mind-product tool find_symbol \
  --arguments '{"name":"commit","limit":20}'
shared-mind-product tool impact_path \
  --arguments '{"symbol_id":"symbol_...","direction":"INCOMING"}'
```

Available tools are returned by `capabilities`; clients should discover rather
than assume the list.

## 7. Shared Skill lifecycle

Skill candidates may be created by extraction or imported from a verified
package. List and inspect them:

```bash
shared-mind-product skill list
shared-mind-product skill show skill:migration-review --version 1
```

Revise with optimistic version checking:

```bash
shared-mind-product skill revise skill:migration-review ./changes.json \
  --expected-version 1
```

Record explicit passing test evidence and approve:

```bash
shared-mind-product skill mark-tested skill:migration-review 1 \
  --evidence '{"passed":true,"runner":"integration-suite","run_id":"..."}'
shared-mind-product skill approve skill:migration-review 1 \
  --approval '{"by":"human:owner","reason":"validated in fixture"}'
```

Failed evidence cannot promote a Skill. Only TESTED or APPROVED Skills are
executable, and only APPROVED Skills are selected by default for context.

Export or import a portable package:

```bash
shared-mind-product skill export skill:migration-review ./migration.skill.zip
shared-mind-product skill import ./migration.skill.zip
```

## 8. Governance and integrity

```bash
shared-mind-product catalog
shared-mind-product review-queue
shared-mind-product verify
shared-mind-product metrics memory-quality
```

`verify` checks the kernel ledger, product audit chain, Skill replay, and a fresh
canonical rebuild of managed derived views.

## 9. Backup and restore

```bash
shared-mind-product backup export ./shared-mind.backup.zip
shared-mind-product backup restore ./shared-mind.backup.zip ./restored-memory
```

Restore requires an empty destination and fails closed on unsafe or mismatched
ZIP entries.

## 10. Product MCP

Install the optional extra:

```bash
python3 -m pip install -e '.[mcp]'
shared-mind-product-mcp --workspace ./memory
```

The MCP surface provides ingest, extraction, Draft review/commit, build,
Task-aware Context, search, generic on-demand memory tools, Skill promotion,
cold start, and verification. Paths are relative to the fixed workspace.

## 11. Local web control

```bash
shared-mind-web --workspace ./memory --host 127.0.0.1 --port 8126
```

Only loopback bindings are accepted. The UI/API reads catalog, queue, and
integrity state and performs Draft and Skill actions through `ProductService`;
it does not write SQLite directly.

## 12. Post-task compounding

```bash
shared-mind-product capture DEV-081 ./task-trace.json --auto-commit
```

For real session capture, the JSON document must satisfy `TASK_TRACE`
(`task-trace@1`) and contain ordered `TASK`, `TOOL`, `RESULT`, `DECISION`,
`FAILURE`, and/or `TEST` events. The trace becomes an immutable source, exact
duplicates return `UNCHANGED`, and reuse of a trace ID with different bytes is
rejected. New candidates enter staging, approved deterministic changes use the
same canonical Proposal boundary, and affected views/indexes are consolidated
incrementally. See [DEV-081 Real Session Capture](DEV-081-real-session-capture.md).
