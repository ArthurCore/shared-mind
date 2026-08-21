# DEV-102 — Hook-based Automatic Observation Capture

> **Project has state. Agents come and go.**
>
> **관찰은 자동, 정본 승격은 검문.**

상태: **DONE (local gates)**

## Public CLI contract

`shared-mind-product` exposes one deterministic observation surface:

```console
shared-mind-product observe start --session <opaque-session-id> --task <task-id>
shared-mind-product observe append --session <opaque-session-id> --event-json '<json>'
shared-mind-product observe finalize --session <opaque-session-id>
shared-mind-product observe prune --before <RFC3339-UTC-cutoff>
```

`start` creates exactly one `observations/pending/<session-digest>.jsonl` file.
The first line is deterministic session metadata and subsequent lines are canonical
`TASK_TRACE_EVENT` (`task-trace-event@1`) values. Repeating `start` with the same
session metadata is `UNCHANGED`; binding the same session to different metadata is
`OBSERVATION_SESSION_CONFLICT`.

Opaque session strings are storage locators, not memory partition keys. A value that
already satisfies the shared semantic-ID contract is preserved. Other values receive
a deterministic `session:<sha256-prefix>` identity. The trace identity is derived from
the same session input, so no clock or Agent identity enters canonical bytes.

`append` validates the complete event before opening the file for append. Schema or
sequence failure leaves the buffer bytes unchanged. Valid events are written as one
canonical JSONL line through `O_APPEND`; the adapter does not replace or generate an
event timestamp.

`finalize` derives `started_at` and `ended_at` from the first and last original events,
builds one `TASK_TRACE` (`task-trace@1`), and calls only
`ProductService.post_task_capture`. It moves the buffer to
`observations/captured/` only after that existing DEV-081 boundary returns. Repeating
finalize reuses the captured buffer and returns the DEV-081 `UNCHANGED` receipt.

Captured observation buffers have unlimited retention by default. `prune` is the
only deletion surface: it scans recognized regular files directly under
`observations/captured/`, validates every buffer before deleting any, and removes
only buffers whose last original event timestamp is strictly before the cutoff.
Buffers exactly at or newer than the cutoff and every pending buffer remain. The
result contains stable `scanned`, `removed`, and `retained` counts. Pruning never
opens or changes the kernel, canonical sources, ledger, receipts, or product store.

## Claude Code hook adapter

`python -m shared_mind.adapters.claude_code_hooks` accepts `append` and `finalize`.
The adapter accepts a validated event in the hook payload's `event` field. For a raw
`PostToolUse` payload it requires an original `occurred_at` or `timestamp`, retains
the tool data in `details`, and derives only stable non-time identity/order fields.

`PostToolUse` also lazily creates the pending buffer when no pending or captured
buffer exists. A schema-valid payload `task_id` is preserved. If the standard hook
payload has no valid task ID, the adapter uses the deterministic Agent-neutral
`observation-<session-sha256-prefix>` task ID. It contains no Agent identity or clock.
An explicitly pre-started buffer always keeps its existing task metadata.

The wrapper is fail-open for Agent execution: any workspace, payload, schema, lock,
or registration failure returns process status 0, emits one stderr line, and writes a
deterministic record under `observations/failed/`. When the workspace is unavailable,
that directory is rooted at the hook working directory. Once a workspace is resolved,
failures are recorded in that workspace. A failed finalize never archives the pending
buffer, so the same bytes can retry through DEV-081.

`shared-mind setup --install-hooks` is explicit opt-in. It atomically merges
`PostToolUse`, `SessionEnd`, and `Stop` command entries into the project's
`.claude/settings.json`, preserves unrelated settings, rejects malformed/conflicting
settings, and is idempotent. Setup without the flag does not read, create, or rewrite
the settings file.

The packaged `shared-mind-setup` Codex skill retains its existing invocation policy
and adds only the session-finalize instruction for a capture that was already started.

## Preserved boundaries

- All Agents and sessions still share one workspace and one canonical Shared State;
  `session_id` is not a canonical partition.
- Hook events are immutable raw evidence. They do not directly create or change a
  Claim, Decision, Question, WorkItem, or Skill.
- Canonical source registration, immutable conflict detection, receipts, and product
  audit remain the DEV-081 `post_task_capture` path.
- Same trace bytes remain idempotent; changed bytes under the same trace identity fail
  closed with `TASK_TRACE_IMMUTABLE_CONFLICT`.
- Source registration and extraction/consolidation failures preserve retryable files
  and accepted source identities without partial replacement.
- DEV-103 web observation routes and DEV-104 review promotion are not part of this
  change. Existing loopback-only web behavior is untouched.

## Acceptance

The eight original acceptance guarantees plus the independent-review lazy-start and
retention guarantees are indexed in
[`testing/dev-102-auto-observation-capture.tdd.md`](testing/dev-102-auto-observation-capture.tdd.md).
