# DEV-101 Natural-language Shared Mind Setup — TDD Evidence

## User journey

A freshly started Codex session receives only this user request:

```text
Shared Mind 초기설정해
```

The session must discover the global `shared-mind-setup` Skill, run one
deterministic setup command, reuse or create exactly one sibling Shared State,
verify integrity, and restore task-aware context without a virtualenv command,
workspace path, or project-history explanation from the user.

## RED

The first RED commit, `6271f3f`, added the setup CLI, Skill/package, idempotency,
conflict, project-root, partial-cold-start, and documentation contracts before
production support existed. All seven initial tests failed.

Two real dogfooding failures were then preserved separately:

- `149157e`: setup detected a valid canonical head with stale disposable views
  but returned `PRODUCT_INTEGRITY_INVALID` instead of consolidating them.
- `3ed6666`: forty mandatory WorkItems overflowed the router's 24 KiB core
  allocation and returned `CONTEXT_BUDGET_TOO_SMALL`.

The failures were not bypassed by weakening integrity or increasing every
request to the 128 KiB ceiling.

## GREEN

Commits `f9d3e2b`, `2f7f105`, `4098804`, and `6fa2e96` implement the setup
surface and its two dogfooding fixes:

- deterministic `shared-mind setup` project/workspace discovery;
- packaged global Skill installation with unmanaged-change protection;
- one completed cold start with safe idempotent retry;
- fail-closed kernel, product-audit, Skill-replay, and provenance verification;
- repair only when disposable derived views alone are stale; and
- start at 24 KiB, then compute only the minimum larger outer budget required
  by mandatory continuity, capped at 128 KiB.

```console
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_natural_language_setup \
  tests.test_agent_bootstrap \
  tests.test_session_ux \
  tests.test_memory_views_product -v
```

Result: 28 tests, 0 failures.

## Full regression and quality gates

The Python 3.13 parallel branch-coverage runner completed 63 test files:

```text
TOTAL files=63 tests=508 failures=0 seconds=21.163
TOTAL branch coverage=83%
```

Both executable contract validators passed. `compileall`, Ruff, configured
mypy (including `setup.py`), and Bandit passed. Strict audit of the installed
non-editable dependency set reported no known vulnerabilities. The Skill
Creator quick validator returned `Skill is valid!`.

An isolated PEP 517 build produced both wheel and sdist; `twine check` passed
with only the existing missing-long-description warnings. The final wheel
SHA-256 was
`879ed2cfbfdd0906ddaf6a500ac34b0a9eb1e7663a20c210db08e777c5323231`, and
both packaged Skill files were present. A fresh `uv` Python 3.13.2 environment
installed only that wheel and passed two setup invocations: the first created
the workspace and cold-started it; the second reused both workspace and Skill
without advancing the ledger.

## Real Shared Mind dogfooding

Running `shared-mind setup` from this repository against
`../shared-mind-memory` returned:

- `SETUP_READY` and `integrity.valid=true`;
- existing workspace reused and completed cold start reused;
- global Skill status `UNCHANGED` at
  `~/.codex/skills/shared-mind-setup`;
- ledger sequence 208 and state root
  `sha256:6eb23ecca87343dabe6e1ddc2ca23a8254643c3c2a95a32f159082b29e2f00a0`;
- 26,181 included bytes in a dynamically computed 26,248-byte budget, rather
  than filling the 128 KiB ceiling; and
- both active P0 WorkItems restored, including DEV-101.

## Fresh-session forward test

A new `codex exec` process (thread
`01a005da-2470-7132-a302-eddade9b23c8`) was started in a new empty Git project.
Its entire user prompt was exactly `Shared Mind 초기설정해`.

The new process independently:

1. selected and read the installed `shared-mind-setup` Skill;
2. executed `shared-mind setup` once;
3. received `SETUP_READY` with valid product/kernel integrity;
4. created exactly one `<project>-memory` sibling workspace; and
5. reported the recovered state without any added task instructions.

The process exited normally after its response; no test Codex session remained
running. The temporary workspace had no project records, so the restored
continuity lists were correctly empty rather than invented.

## Shared State capture and closeout

Strict trace `trace:dev-101-natural-language-setup-20260815-001` was captured
through the public product CLI into the same `../shared-mind-memory` workspace.
It preserved ten ordered TASK/TOOL/DECISION/FAILURE/TEST/RESULT events as source
revision `revision_8d4ae87a0373f4f824674881a2ef2631`; extraction completed with
zero failures and no canonical Draft was inferred from the raw evidence.

A separately validated, version-guarded kernel Proposal moved
`workitem_dev_101_natural_language_setup_001` from TODO v1 to DONE v2 at ledger
sequence 210. Final consolidation, product verification, and explicit ledger
replay passed:

- product result `PRODUCT_INTEGRITY_VALID`;
- state root
  `sha256:cd1e7e996c715a138f1b55c1c8972608da4ff86736a1e9abff8cb6810d6b647d`;
- head hash
  `sha256:a69e9243f61dad51f1e3adb7f7704b21845d7c35d35691bdb4a8d372f95dad80`;
- replay checked all 210 entries with no errors; and
- the next setup context excluded completed DEV-101 and exposed the remaining
  DEV-100 blocker for lifecycle review.

That blocker referred only to environment-dependent gates which this same
unrestricted run had now passed. A second version-guarded Proposal therefore
moved DEV-100 from BLOCKED v2 to DONE v3 instead of carrying stale work into the
next session. The final verified/replayed workspace is at ledger sequence 211,
state root
`sha256:31ea4717d7387041a8302fba7fe2b2c0edc8ff21cd5bb2505c61929539f7ed89`,
and head hash
`sha256:434e90ff51c8df44747eb9cc1af26bbeb06ed2d8f56f62c5e74d98e85f6f1679`.
The final setup context has no stale active WorkItem and fits in 24,509 bytes of
the default 24 KiB budget.

## Preserved boundaries

- One-time software installation is still required; natural language cannot
  execute software that is absent from the machine.
- Setup creates no Agent-specific memory and never edits SQLite directly.
- Scenario, Core Context, Wiki, and indexes remain disposable projections.
- Canonical state continues through validated Proposals and the append-only
  ledger only.
- An unmanaged or user-modified global Skill is never overwritten.
