# DEV-105 -- Project-scoped Automatic Session Restore

> **Project has state. Agents come and go.**
>
> **The working directory selects the project; the project selects exactly one Shared State.**

Status: **IMPLEMENTED; LOCAL GATES PASS (fresh-host dogfooding pending)**

## Contract

`shared-mind setup --install-hooks` creates or reconciles the project-local binding:

```text
<project>/.shared-mind/project-binding.json
```

The binding is `project-binding@1`:

```json
{
  "binding_version": "project-binding@1",
  "project_root": "/physical/project/root",
  "workspace_root": "/physical/project-memory/root",
  "workspace_config_hash": "sha256:..."
}
```

Automatic session bootstrap starts from the hook payload `cwd` or process cwd,
resolves the nearest physical Git root, reads only that root's binding, verifies
the exact project root, verifies the exact workspace config hash, opens exactly
that workspace, verifies product/kernel integrity, and then emits a deterministic
24 KiB EVIDENCE context pack.

It never searches neighboring `*-memory` directories at runtime. Missing,
malformed, symlinked, mismatched, moved, or integrity-invalid bindings return a
stable skipped status with no `additional_context`. Host startup remains
fail-open; canonical/product writes remain fail-closed.

The binding document is a closed shape: extra fields are invalid. The binding
file, its control directory, and the workspace control directory/config must be
regular non-symlink paths. Setup without an explicit workspace reuses only a
verified current-root binding or the exact sibling of the nearest Git root; it
never calls broad ancestor discovery. An existing different binding cannot be
overwritten through the binding writer. `setup --workspace` is the sole rebind
authority.

## CLI and hook surfaces

The client-neutral bootstrap CLI is:

```console
shared-mind session start [--cwd PATH] [--binding PATH]
shared-mind session prompt --prompt TEXT [--cwd PATH] [--binding PATH]
```

`session start` uses the default continuation task and query. `session prompt`
uses the prompt as the task-aware selector input while staying inside the same
verified binding.

Claude Code and Codex generated hooks both use the installed portable entrypoint:

```console
shared-mind-session-hook <claude|codex> <start|prompt|append|finalize>
```

On success the adapter writes:

```json
{
  "continue": true,
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "..."
  }
}
```

For `UserPromptSubmit`, `hookEventName` is `UserPromptSubmit`. The
`additionalContext` bytes are client-neutral; the same binding, state, request,
selector version, and budget produce the same context hash for Claude and Codex.
Append and finalize ignore legacy/stale workspace arguments and resolve the
workspace from the hook payload cwd's verified binding before capture.

## Setup behavior

With `--install-hooks`, setup now installs or reconciles:

- `.shared-mind/project-binding.json`;
- `.claude/settings.json` entries for `SessionStart`, `UserPromptSubmit`,
  `PostToolUse`, `SessionEnd`, and `Stop`;
- `.codex/hooks.json` entries for `SessionStart`, `UserPromptSubmit`,
  `PostToolUse`, and `SessionEnd`.

Codex lifecycle event arrays live under the official top-level `hooks` object;
the top-level `description` and unrelated metadata are preserved. Setup also
migrates the earlier DEV-105 root-level lifecycle entries into that nested
object when encountered.

Codex `SessionStart` and `UserPromptSubmit` entries set
`additionalContextLimit` to `12000`, enough for the 24 KiB bootstrap context
contract as a bounded approximate token threshold, not a byte count. Codex
project hooks still require the normal Codex trust review before execution.

The three project-local destinations are preflighted and built in memory, then
staged and replaced in fixed Claude -> Codex -> binding order. Binding publication
is the activation switch. Failure after any replace restores every pre-existing
file byte-for-byte and removes every newly created destination. Unrelated hook
entries remain present. Generated commands contain no absolute executable,
project, or workspace path.

Because the binding records physical machine-local absolute paths,
`/.shared-mind/project-binding.json` is gitignored and must be recreated by
one-time setup on another checkout or machine.

Setup without `--install-hooks` does not create or rewrite the binding, Claude
settings, or Codex hook file.

## Preserved boundaries

- One canonical Shared State is shared by all agents, models, roles, and sessions.
- No Agent/client-specific memory partition is introduced.
- LLM output never writes canonical state directly.
- Runtime bootstrap does not create workspaces, repair integrity, or search
  outside the verified project binding.
- DEV-081 idempotency/fail-closed behavior remains unchanged.
- DEV-102 observation collection remains fail-open only at the hook wrapper.
- Loopback-only web behavior is unchanged.

## Acceptance

The acceptance guarantees and RED/GREEN evidence are indexed in
[`testing/dev-105-project-scoped-auto-resume.tdd.md`](testing/dev-105-project-scoped-auto-resume.tdd.md).

Fresh Claude/Codex process dogfooding is intentionally not claimed by this
contract record yet; it remains the final operator validation after local gates.
