# Local MCP adapter

Shared Mind provides an optional, local stdio MCP adapter for Codex, Claude
Desktop, Claude Code, and other MCP clients. The adapter binds to one workspace
at startup and reuses the same deterministic service and kernel semantics as the
Python and CLI interfaces.

## Installation

Install once from the repository checkout with `uv`:

```console
$ uv tool install --editable '.[mcp]'
```

`uv` chooses a compatible Python 3.11+ runtime and owns the isolated tool
environment. No manual virtualenv creation or activation is required. Run
`uv tool update-shell` once if `shared-mind-mcp` is not yet on `PATH`.

The base distribution does not include the MCP SDK; the primary session install
above deliberately selects the optional `mcp` extra. The optional dependency is
pinned to `mcp>=2,<3`. Keep that major-version pin when reproducing an
environment because MCP transport APIs and generated schemas may change across
major versions.

The supported release and fresh-wheel surface is MCP SDK v2. The legacy v1
fallback is compatibility code exercised against a simulated SDK import and
registration surface; it is not an installable project extra or fresh-wheel v1
certification.

## Codex project configuration

The checked-in `.codex/config.toml` enables multi-agent support and declares a
project-local `shared_mind` stdio server. It launches `shared-mind-mcp` with
`--workspace ../shared-mind-memory`, uses `cwd = "."`, and starts with
`required = false`. This follows the self-dogfooding boundary: canonical memory
lives beside the repository instead of inside it. No secret, environment
variable, personal absolute path, or remote endpoint is stored in the project
configuration.

The `explorer`, `reviewer`, and `docs_researcher` roles load project-local TOML
layers by standalone-file discovery from `.codex/agents/`. Each layer declares
the required `name`, `description`, and `developer_instructions`, uses a
read-only sandbox, and explicitly forbids file edits and canonical mutations.
Do not repeat these files as `.codex/agents/...` `config_file` overlays in the
project config: relative overlay paths are resolved from `.codex/config.toml`.

Treat the repository and its workspace content as trusted local input before
enabling the server. `required = false` makes an unavailable optional adapter a
non-fatal startup condition; it is not a security control.

## Tools

The adapter exposes exactly this tool allowlist:

| Tool | Behavior |
|---|---|
| `context` | Build a deterministic, budgeted handoff context pack. |
| `query` | Query the deterministic public projection. |
| `proposal_validate` | Validate an inline Proposal without canonical mutation. |
| `proposal_commit` | Submit an inline Proposal to the deterministic kernel. |
| `source_add` | Register a UTF-8 source using a path relative to the source root. |
| `conflict_list` | List canonical conflicts, optionally by lifecycle status. |
| `ledger_verify` | Verify the ledger chain and materialized state root. |

`context`, `query`, `proposal_validate`, `conflict_list`, and `ledger_verify` are
read-only. `proposal_commit` and `source_add` can write local canonical state, so
obtain explicit user approval for each write-capable call. A `FACT_CONFLICT`
result is an accepted, history-preserving success; a transaction or validation
conflict is reported as an MCP tool error without silently applying the change.

Tool annotations are hints; they do not enforce authorization or approval.
Client policy, project trust, and explicit user approval remain the enforcement
boundary. Remote publishing, pushing, messaging, credential changes, and other
external actions remain outside this local adapter and require their own approval.

## Live interoperability evidence

The sanitized live MCP interoperability artifact is checked in at
`evals/product_continuity/results/mcp-interoperability-live-2026-08-12.json`.
It records aggregate evidence only: pinned client/model versions, allowed tool
counts, accepted ledger outcomes, final ledger verification, replay parity, and a
digest over synthetic config/context. It does not retain raw model text, provider
usage or cost data, request identifiers, credentials, account identifiers, or
absolute local paths.

The 2026-08-12 run used the supported MCP SDK v2 surface. Codex CLI 0.147.0 with
the gpt-5.5 service snapshot required destructive MCP approval to be
preauthorized for the single `proposal_commit` call while leaving the allowlist
limited to `proposal_commit` and `ledger_verify`. Claude Code 2.1.227 with
claude-sonnet-4-5 safe mode removes explicit MCP approval prompts, so the run
used the same strict allowlist plus the client-side built-in denylist. Each
client committed exactly one `TODO` work item and then verified the ledger.

## Resources

The adapter exposes exactly these six fixed resource URIs:

| Resource URI | Media type |
|---|---|
| `shared-mind://workspace/info` | `application/json` |
| `shared-mind://workspace/context` | `application/json` |
| `shared-mind://projection/project.json` | `application/json` |
| `shared-mind://projection/project.md` | `text/markdown` |
| `shared-mind://contract/schema` | `application/json` |
| `shared-mind://contract/predicate-registry` | `application/json` |

The resource surface does not expose arbitrary database, SQL, or file resources.
There are no file URI, SQLite URI, workspace-path, or SQL resource templates.
For ingestion, `source_add` accepts only a relative path beneath the configured
source root; absolute paths, parent traversal, and symlink escape are rejected.

## Trust, transport, and logging

The adapter is local-first and does not authenticate a remote identity. Its
workspace selection is fixed when the stdio process starts. Restart it to bind a
different workspace rather than accepting a workspace or database path per call.

Stdout is reserved exclusively for JSON-RPC protocol frames; diagnostics go to
stderr. Do not add ordinary prints, banners, progress bars, or logging handlers
that write to stdout. A client should treat malformed stdout as a transport
failure rather than trying to recover a partially framed response.

## Claude Desktop and Claude Code

Claude Desktop and Claude Code both accept an MCP stdio server definition. This
generic example does not write or modify any external client configuration; copy
or adapt it only after reviewing that client's current settings and trust model:

```json
{
  "mcpServers": {
    "shared-mind": {
      "command": "shared-mind-mcp",
      "args": ["--workspace", "."]
    }
  }
}
```

Launch the client with the Shared Mind project as its working directory when
using `.`. Keep the workspace fixed for the lifetime of that server process.

## Troubleshooting

- Confirm the runtime is Python 3.11 or newer and run `shared-mind-mcp --help`.
- If the `mcp` module is unavailable, repeat the `uv tool install` command with
  `--force` and verify that the
  resolved SDK version satisfies `mcp>=2,<3`.
- If the workspace cannot be opened, initialize it first and confirm the MCP
  process working directory is the intended project root.
- If the client reports malformed protocol output, reserve stdout for JSON-RPC
  and move every diagnostic or logging message to stderr.
- If generated tool metadata changes after an upgrade, verify the MCP SDK
  version pin before changing Shared Mind's seven-tool or six-resource contract.
