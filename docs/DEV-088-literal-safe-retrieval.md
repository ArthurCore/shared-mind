# DEV-088 — Literal-safe retrieval queries

## Problem

The first post-DEV-087 cold-start attempted to find the next task by searching
the real sibling workspace for `DEV-088` and `schema 1.3`.  Both strings were
passed directly to SQLite FTS5 `MATCH`, where `-`, `.`, `OR`, quotes, and
parentheses are query-language syntax.  The public search path therefore
returned `INTERNAL_ERROR` instead of evidence:

```text
DEV-088    -> OperationalError: no such column: 088
schema 1.3 -> OperationalError: fts5: syntax error near "."
```

Task identifiers and version strings are normal Shared Mind evidence, not an
advanced FTS query language.  A fresh session must be able to search them
without knowing SQLite grammar.

## Contract

`retrieval-index@2` defines public lexical queries as **literal Unicode text**:

1. trim the supplied query;
2. split it into Unicode letter/number tokens using the same punctuation
   boundaries as the default SQLite `unicode61` tokenizer;
3. case-fold and de-duplicate tokens while preserving first occurrence;
4. quote every token before binding the resulting FTS expression; and
5. return no results for a query containing no searchable token.

The dependency-free fallback consumes the same normalized token sequence.
Words such as `OR`, `NOT`, and `NEAR` are terms.  Hyphens, periods, quotes,
parentheses, apostrophes, SQL-looking text, and punctuation cannot become FTS
operators or column selectors.

Every Python, CLI, and product MCP search response includes
`retrieval_version: "retrieval-index@2"`.  The original user query remains in
the response for audit and display; only the internal bound MATCH expression is
normalized.

## Boundaries

- Retrieval documents and indexes remain disposable product projections.
- This change does not mutate kernel state, evidence, Claims, Decisions,
  Questions, or WorkItems.
- It does not expose advanced FTS syntax.  A future advanced-search surface
  would require a separate explicit contract and sandbox.
- It does not add a model, embedding provider, vector database, or
  client-specific memory.
- The existing optional vector ranker still receives the original query; only
  the local lexical candidate path is literal-normalized.

## Acceptance

- `DEV-088`, `schema 1.3`, Korean hyphenated text, operators, quotes,
  parentheses, `C++`, SQL-looking text, and punctuation-only input never raise
  an SQLite parser error.
- Task/version queries retrieve indexed source evidence.
- FTS5 and the dependency-free fallback use the same literal token semantics.
- Python, `shared-mind-product search`, and product MCP return the same ordered
  document IDs and `retrieval-index@2` version.
- Existing lexical, vector/RRF, source-span, context, governance, and interface
  regressions remain green.

RED/GREEN, full regression, and real sibling-workspace evidence are recorded in
[`testing/dev-088-literal-safe-retrieval.tdd.md`](testing/dev-088-literal-safe-retrieval.tdd.md).
