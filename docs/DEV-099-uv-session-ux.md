# DEV-099 — uv-first Session Resume UX

> DEV-100 supersedes only the default packing budget: `resume` now defaults to
> 24 KiB and retains 128 KiB as an explicit safety ceiling. DEV-099's workspace
> discovery, integrity, and one-command session contracts are unchanged.

## Problem

The previous primary path asked a user to create and activate a virtual
environment, install with pip, repeat an external workspace path, and provide a
long task/query/budget command. Those mechanics are appropriate as low-level
controls, not as the default session experience.

## User journey

From a project checkout, install once:

```console
$ uv tool install --editable '.[mcp]'
```

Then resume any later coding-agent session with:

```console
$ shared-mind resume
```

No shell alias and no manual virtualenv activation are part of the contract.
`uv` owns the isolated tool environment. If its executable directory is not yet
on `PATH`, `uv tool update-shell` is a one-time shell setup step.

## Workspace selection

Workspace selection is deterministic:

1. An explicit `--workspace PATH` is opened exactly as supplied and never falls
   back to discovery.
2. Without the flag, Shared Mind searches the current path and its parents for
   `.shared-mind/workspace.json`.
3. If none exists, it searches for a valid `<project>-memory` sibling while
   walking from the current directory toward its ancestors.
4. If no valid workspace exists, the command returns `WORKSPACE_NOT_FOUND` and
   does not initialize or mutate anything implicitly.

The sibling convention keeps canonical state outside the Git checkout while
letting commands run from the project root or a nested source directory.

## Resume semantics

`shared-mind resume [TASK]` performs, in order:

1. kernel, product audit, Skill replay, and derived-view integrity verification;
2. fail-closed `PRODUCT_INTEGRITY_INVALID` handling;
3. task-aware EVIDENCE context selection using a 128 KiB default budget;
4. one JSON `SESSION_READY` response containing both `data.integrity` and
   `data.context`.

The default task is `Continue the highest-priority unblocked project work.` A
custom task stays short:

```console
$ shared-mind resume "Review the authentication migration"
```

The existing `shared-mind context` command remains the advanced interface for
custom query, references, depth, byte budget, or token budget. `resume` does not
weaken the Proposal-only canonical mutation boundary; context telemetry remains
product state and no kernel mutation is performed.

## Known limits

- A non-conventional workspace name still requires `--workspace`.
- The current installation is from a checked-out repository until a registry
  release is published.
- `uv tool install --editable` reflects source changes immediately, but a change
  to dependencies requires reinstalling with `--force`.
