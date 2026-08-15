# DEV-088 literal-safe retrieval — TDD and dogfooding evidence

## Source and user journeys

DEV-088 was derived from an actual post-DEV-087 cold-start against
`../shared-mind-memory`, not from a synthetic roadmap placeholder.  The fresh
session had to find task IDs, versions, and source evidence using natural text.

User journeys:

1. A fresh Agent searches for `DEV-088` or `schema 1.3` and receives evidence
   rather than an FTS parser error.
2. Operators, quotes, parentheses, punctuation, SQL-looking text, and Unicode
   identifiers remain literal data.
3. The default FTS5 path and dependency-free fallback agree on token meaning.
4. Python, CLI, and product MCP expose the same ordered result IDs and pinned
   retrieval semantic version.

## RED

Production code was unchanged when the first checkpoint was executed:

```console
PYTHONPATH=src python3 -m unittest tests.test_literal_safe_retrieval -v
```

Result: **4 tests ran, 8 errors**.  The intended failures included:

```text
OperationalError: no such column: 088
OperationalError: no such column: 검색
OperationalError: fts5: syntax error near "OR"
OperationalError: fts5: syntax error near "\"Quoted\""
OperationalError: fts5: syntax error near "-"
```

Checkpoint: `38431b5 test: define DEV-088 literal-safe retrieval`.

After the literal tokenizer was green, a second semantic-version RED required
`retrieval-index@2` and `retrieval_version` in every transport result.  It
failed with one assertion and one `KeyError` against the previous
`retrieval-index@1` implementation.

Checkpoint: `3656274 test: pin DEV-088 retrieval semantics`.

## GREEN

`ProductStore.search` now derives de-duplicated Unicode letter/number tokens,
quotes each token before FTS5 MATCH, and gives the same tokens to the fallback.
Punctuation-only queries return an empty list.  `RetrievalService.search`
reports `retrieval-index@2` in lexical and hybrid results.

```console
PYTHONPATH=src python3 -m unittest -v \
  tests.test_literal_safe_retrieval \
  tests.test_product_retrieval \
  tests.test_product_interfaces \
  tests.test_memory_views_product \
  tests.test_product_governance_eval
```

Result: **33 tests, 0 failures**.

Checkpoints:

- `f17822b fix: make product retrieval queries literal-safe`
- `8edb315 feat: version literal-safe retrieval semantics`

## Full regression and quality gates

The first full-run command was rejected before execution because its setup used
`rm -rf`; a new `mktemp -d` run directory was used instead.  This was a command
safety failure, not a product-test result.

```console
/private/tmp/shared-mind-dev080-verify-venv/bin/python \
  tools/run_parallel_coverage.py \
  --workers 2 --fail-under 80 \
  --log-dir <mktemp>/logs --summary-log <mktemp>/summary.log
```

Result on Python 3.13.2:

```text
TOTAL files=52 tests=432 failures=0 seconds=32.290
branch-enabled coverage total 83%
```

Additional gates:

- kernel contract: 7 predicates, 16 typed fixtures, 6 negative cases,
  6 semantic cases, 7 continuity operations — PASS;
- product contract: 10 typed fixtures, 14 negative cases — PASS;
- compileall, Ruff, configured mypy scope, Bandit, and `git diff --check` — PASS.

Hosted evidence on head `3db636a4579925a9badce97d189ce6669fb7ddd4`:

- PR run
  [`31869424469`](https://github.com/ArthurCore/shared-mind/actions/runs/31869424469):
  8/8 jobs PASS;
- push run
  [`31869406046`](https://github.com/ArthurCore/shared-mind/actions/runs/31869406046):
  8/8 jobs PASS.

## Real self-dogfooding

The same external workspace used by prior Codex and Claude sessions was kept:
`../shared-mind-memory`.  No Agent-specific state was created.

1. A kernel Proposal created
   `workitem_dev_088_literal_safe_search_001` as `TODO` at ledger sequence 156.
2. A guarded Proposal moved it to `DOING` version 2 at sequence 157.
3. Before the fix, real searches for `DEV-088` and `schema 1.3` failed with the
   parser errors captured above.
4. After the fix, all of these returned `SEARCH_COMPLETED` without error:
   `DEV-088`, `schema 1.3`, `next after DEV-087`, `OR NOT NEAR`, and
   punctuation-only input.
5. Rebuilding the index exposed the DEV-088 WorkItem as an exact search hit.
6. `verify` then correctly detected that the new canonical WorkItem made three
   existing views stale and one work Scenario missing.  `consolidate` rebuilt
   exactly those four artifacts plus the retrieval index.
7. The immutable six-event task trace
   `trace:dev-088-literal-safe-search-20260815-001` was captured as source
   revision `revision_d759b5e51dc9345e79fb4df3abd1deb2`.
8. A guarded Proposal moved the WorkItem from `DOING` v2 to `DONE` v3.
9. Final consolidation and verification returned `PRODUCT_INTEGRITY_VALID`,
   kernel ledger 159, and state root
   `sha256:0307c40df760f335339e891bcb833b254de58bf6fb834566ca98be6a7e465e42`.
10. The next-session summary context hash is
    `sha256:780500c4cfe46104cd0339fd8e755a065c49d58dc9abc5990768675f0d0e96f3`;
    active WorkItems and open Questions are both zero.

The initial WorkItem helper also attempted to use `Kernel` as a context manager;
that failed before any canonical mutation because `Kernel` has no `__enter__`.
The retry used explicit `close()` and then committed through
`WorkspaceService.commit_proposal`.

## Guarantees

| Guarantee | Evidence | Type | Result |
|---|---|---|---|
| Task IDs and versions are literal terms | `test_task_ids_and_versions_are_literal_search_terms` | integration | PASS |
| FTS operators and punctuation cannot escape data | `test_fts_operators_quotes_parentheses_and_symbols_never_escape_query_data` | security/integration | PASS |
| FTS and fallback normalize identically | `test_fts_and_dependency_free_fallback_share_literal_token_semantics` | compatibility | PASS |
| Python/CLI/MCP return the same versioned result | `test_python_cli_and_mcp_return_the_same_literal_search_results` | interface | PASS |
| Existing retrieval and product paths remain green | 33-test targeted command | regression | PASS |
| Whole repository stays above the coverage gate | 432-test parallel runner | full regression | PASS |

## Known boundary

This is intentionally a literal natural-text search surface.  It does not
preserve user-supplied FTS boolean, prefix, column, or NEAR syntax.  Adding an
advanced query language would be a separate versioned feature with explicit
validation and must not weaken this safe default.
