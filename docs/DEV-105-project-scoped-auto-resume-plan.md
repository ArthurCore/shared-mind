# DEV-105 — Project-scoped Automatic Session Restore (Plan)

> **Project has state. Agents come and go.**
>
> **The working directory selects the project; the project selects exactly one Shared State.**

Status: **PLANNED**

## 1. Objective

Starting Claude Code or Codex inside a configured project must restore that
project's Shared Mind context before the first model turn without requiring the
user to run `shared-mind resume`. The first user prompt then refines context for
the task, and observation capture returns to the same selected workspace.

Automatic restore is project-scoped, not a global memory search. A session
started in project A must never inject, search, merge, or mutate project B's
memory unless the user explicitly invokes a future cross-project workflow.

Manual `shared-mind resume` remains an advanced recovery and custom-budget
surface; it is no longer the normal per-session entrypoint.

## 2. User journeys

1. As a Claude Code user, I start a session anywhere under my Git project and
   receive verified project context automatically.
2. As a Codex user, I receive the same context hash from the same project and
   Shared State without a client-specific memory copy.
3. As a project owner, I can keep adjacent projects isolated even when both
   have sibling `*-memory` workspaces.
4. As an integrator, I can reuse one client-neutral bootstrap envelope from an
   API agent or another hook-capable host.
5. As an operator, a missing, ambiguous, moved, or integrity-invalid binding
   starts the AI host without stale context and shows a bounded warning.

## 3. Non-negotiable invariants

- **CWD is the automatic authority boundary.** Resolve the physical cwd and
  select the nearest Git root (`.git` file or directory).
- **Exactly one project binding.** Automatic restore uses only the verified
  binding for that root. It never walks parent sibling memories after finding
  the Git root and never falls back to another project.
- **One Shared State.** Claude, Codex, models, roles, subagents, and sessions do
  not receive canonical partitions.
- **Same input, same context.** The same project binding, state, request,
  selector version, and budget produce the same `context_hash` across clients.
- **Read path fails closed; host startup fails open.** Invalid integrity or
  binding means no context injection. The host may continue with a warning.
- **Writes stay fail closed.** Capture/finalize continues through DEV-081
  immutable registration and existing Proposal/review boundaries.
- **No automatic cross-project context.** Cross-project access is out of scope
  and must require a future explicit user-authorized request.
- Preserve loopback-only web behavior and DEV-102 hook-only fail-open semantics.

## 4. Binding and resolution contract

`shared-mind setup --install-hooks` creates or reconciles one project-local
binding at:

```text
<project>/.shared-mind/project-binding.json
```

Versioned closed shape `project-binding@1`:

```json
{
  "binding_version": "project-binding@1",
  "project_root": "/physical/project/root",
  "workspace_root": "/physical/project-memory/root",
  "workspace_config_hash": "sha256:..."
}
```

Automatic resolution algorithm:

1. Resolve hook/input `cwd` to a physical path.
2. Walk upward to the nearest Git root and stop.
3. Read only that root's binding file.
4. Require exact physical `project_root` equality.
5. Require a regular, non-symlink binding file and workspace control files.
6. Recompute and compare the workspace config hash.
7. Open exactly `workspace_root`; do not call broad ancestor sibling discovery.
8. Verify product/kernel integrity before constructing context.

If any step fails, return a stable non-canonical status and no
`additionalContext`. Setup may create a conventional exact sibling
`<git-root-name>-memory`, but runtime automatic restore never guesses beyond the
stored binding.

Nested directories resolve to the same nearest Git root. A nested Git repo or
submodule is a separate project. A Git worktree (`.git` file) is a separate
local root for binding purposes; setup may explicitly bind it to the original
workspace when the user wants the worktrees to share state.

## 5. Client-neutral bootstrap contract

Add a provider-neutral module and CLI/hook entrypoint:

```text
shared-mind session start [--cwd PATH] [--binding PATH]
shared-mind session prompt --prompt TEXT [--cwd PATH] [--binding PATH]
```

Core result `session-bootstrap@1` contains:

```text
status, phase, project_root, workspace_root, binding_hash,
integrity_valid, context_hash, additional_context, warning
```

No client/model/session identifier enters the `ContextRequest` or context hash.

### SessionStart

Before a task is known, request the compact EVIDENCE bootstrap used by resume:

- task: `Continue the highest-priority unblocked project work.`
- query: `project purpose current decisions open questions active work conflicts evidence`
- budget: 24 KiB

The returned developer context identifies the project/workspace binding and
contains the deterministic Shared Mind context pack.

### UserPromptSubmit

Use the actual first/subsequent user prompt as the task/query input, resolve the
same binding again, and inject only context from that same workspace. No
session-local binding database is introduced.

## 6. Host adapters

### Claude Code

Project `.claude/settings.json` receives idempotent entries for:

- `SessionStart` → bootstrap start JSON with `additionalContext`
- `UserPromptSubmit` → task-aware bootstrap JSON
- existing `PostToolUse` → observation append
- existing `SessionEnd` and `Stop` → finalize

The adapter emits Claude-compatible `hookSpecificOutput` and never prints
context on failure.

### Codex

Project `.codex/hooks.json` receives the equivalent lifecycle configuration.
Codex officially supports `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and
`SessionEnd`; `SessionStart` and `UserPromptSubmit` accept
`hookSpecificOutput.additionalContext`. Set a bounded
`additionalContextLimit` large enough for the 24 KiB contract and require the
normal Codex project-hook trust review.

Reference: <https://developers.openai.com/codex/hooks>

### Other hosts

Other AI models are supported only when their host can run a start/prompt hook,
prepend developer/system context, call an MCP/tool before the first turn, or be
wrapped by a launcher. The neutral CLI/envelope is the integration boundary;
Shared Mind cannot inject into an unmodifiable web chat.

## 7. Failure and security behavior

| Condition | Automatic behavior |
|---|---|
| No nearest Git root | continue host, inject nothing, `PROJECT_ROOT_NOT_FOUND` warning |
| Binding missing | continue host, inject nothing, `PROJECT_BINDING_NOT_FOUND` warning |
| Binding malformed/symlinked | continue host, inject nothing, stable invalid-binding warning |
| Project root mismatch | continue host, inject nothing, `PROJECT_BINDING_MISMATCH` |
| Workspace config hash mismatch | continue host, inject nothing, `WORKSPACE_BINDING_MISMATCH` |
| Product/kernel integrity invalid | continue host, inject nothing, `PRODUCT_INTEGRITY_INVALID` |
| Valid binding and integrity | inject context and expose matching `context_hash` |

Hook errors and bootstrap warnings are non-canonical diagnostics. They do not
create a workspace, repair canonical state, search other projects, or advance
ledger/product audit state.

## 8. File plan

```text
create  src/shared_mind/session_bootstrap.py
create  src/shared_mind/adapters/session_hooks.py
modify  src/shared_mind/cli.py
modify  src/shared_mind/setup.py
modify  src/shared_mind/adapters/claude_code_hooks.py (compatibility wrapper if needed)
create  tests/test_project_session_bootstrap.py
create  tests/test_session_hook_adapters.py
modify  tests/test_natural_language_setup.py
modify  tests/test_session_ux.py (manual resume regression only)
modify  README.md
modify  ROADMAP.md
create  docs/DEV-105-project-scoped-auto-resume.md
create  docs/testing/dev-105-project-scoped-auto-resume.tdd.md
```

## 9. Acceptance tests (RED first)

1. Starting from a project root and nested directory resolves the same binding,
   workspace, context bytes, and `context_hash`.
2. Two neighboring projects with two memory workspaces never cross-load; unique
   marker records from project B are absent from project A's bootstrap.
3. A nested Git repository selects its own binding and never the parent binding.
4. Missing binding never falls back to an ancestor or neighboring
   `*-memory` workspace.
5. Malformed/symlinked binding, root mismatch, workspace-config hash mismatch,
   and invalid integrity each return success to the hook host with no
   `additionalContext` and no canonical/product mutation.
6. Claude and Codex SessionStart adapters return byte-identical
   `additionalContext` and the same `context_hash` for the same binding.
7. UserPromptSubmit refines context using the prompt only within the verified
   binding; a changed cwd/binding injects nothing.
8. Observation append/finalize uses the workspace in the verified binding and
   cannot write to a different project's workspace.
9. `setup --install-hooks` atomically and idempotently installs the binding,
   Claude lifecycle hooks, and Codex lifecycle hooks while preserving unrelated
   settings/hooks.
10. Setup without `--install-hooks` touches none of the binding or hook files.
11. Existing manual `shared-mind resume`, DEV-081 idempotency/conflict behavior,
    and DEV-102 capture tests remain green.
12. Cross-client deterministic subset covers bootstrap context parity.

## 10. Implementation stages and dependency graph

```text
Stage 1: strict project binding + neutral bootstrap
    ↓
Stage 2: Claude/Codex hook adapter output parity
    ↓
Stage 3: idempotent setup installers and compatibility
    ↓
Stage 4: docs, real-session dogfooding, full verification
```

Stages are intentionally serial because each later stage consumes the previous
stage's contract. Do not start a stage before its focused acceptance gate is
GREEN.

## 11. Verification gates

For every stage:

```bash
python3 contracts/validate_contract.py
python3 contracts/validate_product_contract.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Final dogfooding must prove:

- a fresh Claude Code session started under this repository receives context
  automatically;
- a fresh Codex session under the same repository receives the same context hash;
- a fresh session under a second fixture project cannot observe this project's
  unique memory marker;
- all captured events return to the originally bound workspace;
- no manual `shared-mind resume` command is used in those forward tests.

## 12. Non-goals

- Global search or automatic aggregation across multiple project memories.
- Agent/model-specific canonical state or separate Claude/Codex memories.
- Automatic workspace creation during SessionStart.
- Silent integrity repair during bootstrap.
- Automatic context injection into hosts with no hooks, MCP/tool preflight,
  launcher, or system/developer prompt control.
- Changing the existing loopback web or Draft promotion authority boundaries.

## 13. Plan mutation and rollback

- Preserve RED checkpoints and stage evidence in
  `docs/testing/dev-105-project-scoped-auto-resume.tdd.md`.
- If a host hook schema differs, change only the host adapter; do not fork the
  neutral bootstrap contract or Shared State.
- If automatic bootstrap causes startup regressions, removing the generated
  project hook entries disables automation without deleting the binding or
  workspace.
- Never weaken project binding checks to make a host adapter test pass.
