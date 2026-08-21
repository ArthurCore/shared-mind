# Coding-agent bootstrap

This guide is the operational contract for a coding agent entering an existing
Shared Mind workspace. Install once from the project checkout; `uv` owns the
isolated tool environment, so no virtualenv activation is required:

```console
$ uv tool install --editable '.[mcp]'
```

Run `uv tool update-shell` once if the command is not yet on `PATH`.

## Natural-language initial setup

From a Git project, run the idempotent setup once:

```console
$ shared-mind setup --install-hooks
```

It reuses a verified existing project binding or the exact sibling
`<project>-memory` workspace, performs a bounded
deterministic cold start only when one has not completed, installs the global
Codex `shared-mind-setup` skill, writes
`.shared-mind/project-binding.json`, reconciles Claude Code and Codex project
hooks, verifies product/kernel integrity, and returns one `SETUP_READY` JSON
document. It never creates Agent-specific state and never commits model output
implicitly.

Implicit setup stops at the nearest Git root and never discovers an ancestor
project's sibling memory. A malformed, mismatched, or conflicting binding is not
silently overwritten. Rebinding requires an explicit `--workspace` that opens a
valid workspace for the current root.

The Claude settings, Codex hooks, and binding are staged and installed as one
rollback-capable transaction. The binding is published last, and every original
file is restored byte-for-byte if a replacement fails. Generated lifecycle
commands use `shared-mind-session-hook`; they contain no absolute Python,
project, or workspace path. `.shared-mind/project-binding.json` is machine-local
absolute-path state and is gitignored.

Every later Claude Code or Codex session should be started from somewhere inside
the Git project. The working directory selects the nearest Git root, and that
root's project binding selects exactly one Shared Mind workspace. The
SessionStart hook injects compact project context before the first model turn;
the UserPromptSubmit hook refines context with the actual prompt. Codex
project hooks require the normal trust review before they execute.
PostToolUse and session-finalization hooks also use the neutral adapter, resolve
the binding again from the payload cwd, and cannot be redirected by a stale
workspace argument.

The global setup skill still recognizes `Shared Mind 초기설정해` as a one-time
natural-language way to run setup from Codex after the package is installed. It
is not a per-session resume step.

## Automatic and Manual Bootstrap

The hook-neutral bootstrap surface is:

```console
$ shared-mind session start
$ shared-mind session prompt --prompt "Review the authentication migration"
```

Both commands read only the current Git root's
`.shared-mind/project-binding.json`. They do not search neighboring
`*-memory` directories and they return no `additional_context` if the binding is
missing, ambiguous, moved, or integrity-invalid.

Manual resume remains available for recovery and custom budgets:

```console
$ shared-mind resume
$ shared-mind resume "Review the authentication migration"
```

`resume` discovers the workspace from the project tree, verifies kernel and
product integrity, and emits exactly one JSON document. Require `ok: true`, code
`SESSION_READY`, and `data.integrity.valid: true`, then read `data.context`.
The default is a compact 24 KiB EVIDENCE request. The explicit full resume
ceiling remains available for evidence-heavy work:

```console
$ shared-mind resume --budget-bytes 131072
```

For custom selectors, token budgets, or more than 128 KiB use the advanced
command:

```console
$ shared-mind context --task "Review the authentication migration" --budget-tokens 4096
```

If the advanced context response is `CONTEXT_BUDGET_TOO_SMALL`, increase the budget. Shared Mind
will not hide an open conflict, active decision, open question, or actionable
work item merely to fit a requested budget. If truncation
metadata lists omitted records, follow its projection references before making
a decision that depends on them.

`--budget-tokens` uses the deterministic estimator declared in truncation
metadata and reports `token_estimate_exact: false`. For a hard model-specific
token ceiling, use the optional `exact-token-counter@1` Python protocol with a
pinned tokenizer name, version, fingerprint, and model, or run that tokenizer
externally and pass the safe result through `--budget-bytes`. Shared Mind does
not bundle a provider tokenizer. Current context metadata is versioned as
`handoff-context@3` and `context-selection@3`.

For a new workspace, initialize it once:

```console
$ shared-mind init ./memory --purpose "Preserve this project's reasoning across AI sessions."
```

## Proposal-only mutation boundary

Never edit `.shared-mind/shared-mind.sqlite3` directly. Never treat Markdown or
JSON output as a write API. Canonical mutations must enter through a validated
Proposal so schema rules, evidence checks, version guards, receipts, and the
ledger transaction remain intact. `source add` is safe because it constructs
and commits a `REGISTER_SOURCE_REVISION` Proposal internally.

Markdown and JSON projections are non-authoritative. They are review and query
surfaces that can be regenerated from ledger-backed state. Editing them cannot
change memory and the edits will be replaced by the next projection.

An agent may draft local Proposal JSON, inspect context/projections, and run
read-only Git commands. Do not commit or push projection changes unless the user
asks. Do not publish, merge, or change remote resources without explicit user
approval.

## Standard agent workflow

Place Markdown or UTF-8 text input beneath the workspace `sources/` directory,
then register it:

```console
$ shared-mind source add sources/input.md --source-id document:input
```

Create `proposal.json` as a local draft. It may contain one or more operations,
but every operation must use the pinned schema, predicate-registry,
conflict-rule, guard-DSL, and projection versions exposed by the workspace.
Validate without mutation, then commit the same file:

```console
$ shared-mind proposal validate proposal.json
$ shared-mind proposal commit proposal.json --json
```

Interpret the result before continuing:

| Result code | Exit | Meaning and agent action |
|---|---:|---|
| `COMMITTED` | 0 | Accepted atomically; use the returned ledger sequence and state root. |
| `FACT_CONFLICT` | 0 | Accepted atomically; preserve and review every returned conflict member. |
| `TRANSACTION_CONFLICT` | 4 | No canonical mutation; refresh context and rebase the Proposal. |
| `VALIDATION_ERROR` | 3 | No canonical mutation; correct the reported reason codes and paths. |

Open fact conflicts are successful committed state, not validation failure. Do
not summarize their members as a single settled fact. List open conflicts with:

```console
$ shared-mind conflict list --status OPEN
```

Resolution also goes through an explicit Proposal whose single
`RESOLVE_CONFLICT` operation matches the requested conflict ID:

```console
$ shared-mind conflict resolve conflict_example_12345678 --proposal resolution.json
```

When a transaction conflict includes `data.rebase_hint`, treat it only as an
advisory description of failed and replacement preconditions. It is explicitly
`safe_to_auto_apply: false`: refresh state, review the current aggregate, and
build a new Proposal. The canonical `decision_receipt` remains unchanged.

For deterministic structured reads, the CLI also supports `shared-mind query`
with repeatable `--kind`, `--id`, `--predicate`, `--source-id`,
`--source-revision-id`, and `--status` filters plus title, pagination, and
`--summary-only`. Filter categories are ANDed and repeated values within a
category are ORed. Query output is a non-authoritative projection and cannot
mutate canonical state.

After accepted changes, run the context command again before choosing the next
task.

## Deterministic projection and Git diff workflow

Verify canonical history before generating a review surface, then project it:

```console
$ shared-mind replay --verify
$ shared-mind project --format markdown
$ git status --short -- projections/
$ git diff -- projections/
```

`project` writes `projections/project.md` and also returns its path and content
inside JSON. On the first run the file may be untracked, so `git status` is the
required companion to `git diff`. After a reviewed baseline is tracked, a
ledger change produces a readable projection diff; running `project` again at
the same ledger head and projection version produces byte-identical output.

Review the diff for claims, evidence locators, open conflicts, continuity
records, and history links. Never infer canonical corruption from a projection
edit alone: run `replay --verify`, discard/regenerate the non-authoritative
projection as appropriate, and recover materialized state from the ledger.

For a machine-readable full projection, use:

```console
$ shared-mind project --format json
```

## Local MCP and external adapters

The optional local MCP server exposes the same transport-neutral service
envelopes as the CLI. Its workspace is fixed at startup. `context`, `query`,
`proposal_validate`, `conflict_list`, and `ledger_verify` are read-only;
`proposal_commit` and `source_add` can write canonical state and require the
same explicit user approval as their CLI equivalents. Tool annotations are
hints, not an authorization mechanism. See the [local MCP guide](mcp.md).

The AtomicStrata, Qarinah, and SwarmVault adapters accept already-captured
bytes and create source revisions only by default. They do not contact vendors
or promote imported text to truth. See [external source adapters](adapters.md)
and the local-only [remote policy boundary](remote-policy.md).

Two-process acceptance tests show one winner and one auditable transaction
conflict for competing destructive changes, while independent commutative
evidence attaches are both preserved: silent overwrite is 0 in that automated
scenario. This is not a claim that a paid live Codex+Claude interoperability
evaluation has been run. The checked-in continuity scorer is deterministic and
offline; its live protocol remains opt-in as documented in
[product-continuity dogfooding](dogfooding.md).

See the [SRS](SRS.md) for the authoritative product requirements and lifecycle
semantics.
