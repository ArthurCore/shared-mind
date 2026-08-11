# Coding-agent bootstrap

This guide is the operational contract for a coding agent entering an existing
Shared Mind workspace. It assumes the `shared-mind` console command is installed
and the shell is at the workspace root or below it.

## Resume in one command

Run this before proposing or changing project work:

```console
$ shared-mind context --budget-tokens 4096
```

The command discovers `.shared-mind/workspace.json` by walking upward and emits
exactly one JSON document. Require `ok: true` and `code: "CONTEXT_READY"`, then
read `data.context`. The context contains current unconflicted claims, all open
conflicts and their member claims, active decisions, open questions, actionable
work items, the ledger sequence, the state root, and truncation metadata.

If the response is `CONTEXT_BUDGET_TOO_SMALL`, increase the budget. Shared Mind
will not hide an open conflict merely to fit a requested budget. If truncation
metadata lists omitted records, follow its projection references before making
a decision that depends on them.

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

See the [SRS](SRS.md) for the authoritative product requirements and lifecycle
semantics.
