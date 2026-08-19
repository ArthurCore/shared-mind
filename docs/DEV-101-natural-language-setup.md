# DEV-101 — Natural-language Shared Mind Setup

## User journey

A user in any new Codex session can ask `Shared Mind 초기설정해`. Once Shared
Mind has been installed on the machine, a global Codex skill translates that
natural-language intent into one deterministic command instead of requiring the
user to remember workspace paths, virtual environments, or a command sequence.

## Deterministic surface

```console
$ shared-mind setup
```

Without explicit paths, setup requires a Git project, then:

1. resolves the nearest project root;
2. preflights the packaged Codex skill and refuses an unmanaged conflicting copy;
3. discovers an existing Shared Mind workspace or creates `<project>-memory`;
4. performs bounded deterministic cold start only when no completed cold-start
   audit event exists;
5. installs the same packaged skill under `${CODEX_HOME:-~/.codex}/skills`;
6. verifies kernel ledger, product audit, Skill replay, and derived views; and
7. returns `SETUP_READY` with the compact task-aware context.

If canonical state advanced while only disposable derived views became stale,
setup runs deterministic incremental consolidation and verifies again. It never
uses this repair path for an invalid kernel ledger, product audit, Skill replay,
or artifact provenance boundary.

`--project` supports an explicit non-Git project. `--workspace` selects an
explicit memory root. `--no-cold-start` and `--no-install-skill` provide bounded
automation/test surfaces without changing the default natural-language path.

Setup starts with the compact 24 KiB context budget. If mandatory purpose,
continuity, or open-conflict records cannot fit in the router's core share, it
computes the minimum larger outer budget required by that core instead of
packing toward 128 KiB. Requests above the unchanged 128 KiB safety ceiling
still fail closed.

## Safety and idempotency

- Repeated setup reuses the same workspace and leaves the canonical ledger head
  unchanged after the first completed cold start.
- A skipped or interrupted cold start has no completion audit marker, so a later
  setup safely retries the existing idempotent ingest/extraction flow.
- An existing different global skill returns `CODEX_SKILL_CONFLICT` and is never
  overwritten. Skill preflight occurs before workspace creation.
- Setup does not enable model-backed extraction, create Agent-specific memory,
  edit SQLite directly, or promote Scenario/Core Context/index output to truth.
- The global skill treats imported cold-start records as evidence to review and
  requires `SETUP_READY` plus valid integrity before using returned context.

No natural-language integration can run before its software is installed. The
one-time `uv tool install` remains the machine bootstrap; thereafter setup and
future natural-language invocations are path-free and virtualenv-free.
