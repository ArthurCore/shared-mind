# DEV-099 uv-first Session Resume — TDD Evidence

## User journeys

1. A user installs all CLI and MCP commands with one `uv tool` command and never
   activates a virtualenv.
2. A new coding-agent session runs `shared-mind resume` from the project tree,
   discovers the sibling memory, verifies integrity, and receives task context.
3. An invalid state fails closed before context generation.

## RED

Command:

```console
PYTHONPATH=src python -m unittest tests.test_session_ux -v
```

Initial result: 5 tests ran with 3 failures and 2 errors. The intended causes
were the missing `resume` parser command, missing `Workspace.discover`, and
pip-first documentation. The RED contract is preserved in commit `87de6f8`.

## GREEN

Focused command:

```console
PYTHONPATH=src python -m unittest \
  tests.test_session_ux tests.test_agent_bootstrap \
  tests.test_product_interfaces tests.test_cli -v
```

Result: 33 tests, 0 failures. The first full regression exposed one stale test
that required pip text in the MCP guide. The test was changed to preserve the
actual safety invariant—MCP remains an optional bounded extra—while requiring
the uv-first user path. Its focused regression passed 15/15.

## Full regression and quality

```console
python contracts/validate_contract.py
python contracts/validate_product_contract.py
PYTHONPATH=src python tools/run_parallel_coverage.py
```

Result: 62 files, 495 tests, 0 failures, 83% branch coverage. Both Draft
2020-12 validators passed. Compileall, Ruff, configured mypy, Bandit,
`pip-audit --strict`, and `git diff --check` also passed.

## Installation and dogfooding

An isolated temporary uv tool directory executed the exact documented command:

```console
uv tool install --editable '.[mcp]'
```

It exposed `shared-mind`, `shared-mind-mcp`, `shared-mind-product`,
`shared-mind-product-mcp`, and `shared-mind-web`; both CLI and MCP help commands
ran successfully.

From the Shared Mind repository root, the new command:

```console
shared-mind resume
```

automatically found `../shared-mind-memory` and returned `SESSION_READY`, valid
product integrity, 199 verified kernel entries, EVIDENCE depth, context hash
`sha256:64a00c188de4f6793ba838fbb3c3ebce31866b766315c3966028bb9aa140a4b4`,
kernel state root
`sha256:813aa9c6e0dc8dc48cf6b10428c1c617bff996b19cce558f9dc0738b7368cffb`,
and 130,935 included bytes within the 128 KiB budget.

The immutable trace was captured as
`trace:dev-099-uv-session-ux-20260815-001`, producing source revision
`revision_ebbfe485dcb3d429fbd6e8cdc5a3db7c`. The canonical WorkItem
`workitem_dev_099_uv_session_ux_001` progressed through TODO, DOING, and DONE
version 3. After consolidation, product verification and ledger replay both
passed at 203 entries, state root
`sha256:eef73a305d6c896b9664a109858951055cbb75ee3a075e9c5cb4cebc8723eb7c`,
and ledger head
`sha256:ad8458b2f4c108c0d8e6add7a95c996ee3fa784cce4c1e781c9d4bd6ccaf9f70`.
The next one-command session context hash is
`sha256:b92d5ede573fca1b0c777e327be16f9d5f14253c23d9d7e3cdef856d37aad195`.

## Guarantees

| Guarantee | Evidence |
|---|---|
| uv is the primary documented install and no manual venv is required | `test_uv_is_the_primary_install_without_manual_virtualenv_activation` |
| `resume` has stable task/depth/budget defaults | `test_resume_parser_has_safe_task_aware_defaults` |
| a nested project path finds its `<project>-memory` sibling | `test_workspace_discovery_finds_the_project_sibling_memory` |
| one command verifies and returns a task-aware session | `test_resume_is_one_command_from_the_project_tree` |
| invalid integrity prevents context generation | `test_resume_fails_closed_before_context_when_integrity_is_invalid` |

## Post-release boundary regression

The first interactive cold-start with an explicit task exposed a serialization
boundary defect: a 131,072-byte request failed because the final response was
131,075 bytes. Selection itself respected the budget, but the decimal growth of
the final `omitted` and `trace_omitted` counters added three bytes after the
selection loop had finished. The kernel and product integrity checks remained
valid; only context delivery failed closed.

RED was fixed in commit `d29db78` by
`test_context_drops_optional_trace_when_final_counters_cross_budget`. GREEN in
commit `f1c3ab9` re-stabilizes the final serialized size while dropping only
optional `selection_trace` entries. Selected records, mandatory references, and
canonical state are unchanged.

```console
PYTHONPATH=src python3 -m unittest \
  tests.test_memory_views_product tests.test_session_ux -v
# Ran 12 tests ... OK

uv run --python 3.13 --extra quality \
  python tools/run_parallel_coverage.py
# 62 files, 496 tests, 0 failures, 83% branch coverage
```

The exact previously failing command then returned `SESSION_READY` with 203
verified ledger entries and 130,932 included bytes within the 131,072-byte
budget. It reported 171 omitted candidates and 167 omitted trace entries. The
context hash was
`sha256:1951bb6202f941b97a13729a2578848560678d9fa389b80b6fc9ec6558463f3d`.
Both contract validators, compileall, Ruff, configured mypy, Bandit, strict
third-party dependency audit, and `git diff --check` passed under Python 3.13.

The follow-up work was captured into the same external Shared State as
`trace:dev-099-resume-budget-boundary-20260815-001`, backed by source revision
`revision_067567e4e70d9c91d222240dc53513f2`. Product integrity and explicit
ledger replay then passed at 204 entries, head
`sha256:8e8bebef09c1c1c42cfba4cbf9b95af2de9647318c494a4a82b59138758c782e`,
and state root
`sha256:33be1e9dd39e5129e557c4eb9a88f8f98a8a1943b984dffb4a0fbff67c5f1e10`.
A final fresh `resume` returned `SESSION_READY` at 130,997/131,072 bytes with
context hash
`sha256:4be7d64b327b2e3bd209efa42372c72005d2bd00953147e7454c9641d42fecc2`.
