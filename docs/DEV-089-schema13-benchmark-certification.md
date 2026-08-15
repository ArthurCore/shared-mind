# DEV-089 — Fresh schema 1.3 benchmark certification

## Problem

DEV-021 proved the 100,000-entry context path with schema 1.2 fixtures and later
verified those databases with schema 1.3 code.  That is valuable migration
evidence, but it is not proof that a newly generated current-schema database can
be verified, replayed, and projected at the same scale.  Its combined result was
also assembled from separate commands without a strict result schema.

## Contract

`context-benchmark-certification@1` is a fail-closed, one-command certification:

1. create one deterministic fixture exclusively through `Kernel.commit()`;
2. require the fixture schema to equal the current write schema;
3. run `verify_ledger()` and require manifest/head/state-root parity;
4. replay into a new file and require exact ledger, receipt, head, root, and
   receipt-schema-version parity;
5. run five warm-ups and fifty context samples under a hard byte budget;
6. require a nearest-rank p95 no greater than two seconds; and
7. validate and content-address the portable result before an atomic no-clobber
   write.

The result contains hashes and sizes, not local database paths.  The certification
JSON is evidence and never becomes canonical project truth.

## Acceptance

- Both `history-heavy` and `hot-active` use 100,000 fresh schema 1.3 receipts and
  ledger entries.
- Verification checks all 100,000 entries with no error.
- Explicit file replay exactly matches the source snapshot.
- Every context sample has the same hash and stays within 32,000 bytes.
- Fifty measured samples use the declared nearest-rank p95 and meet the two-second
  target.
- A historical-schema manifest, invalid verifier result, clobber attempt, schema
  drift, or result-hash drift fails closed.
- A strict Draft 2020-12 schema and unit tests validate checked-in results.

The executable evidence and measurements are documented in
[`testing/dev-089-schema13-benchmark-certification.tdd.md`](testing/dev-089-schema13-benchmark-certification.tdd.md)
and [`../benchmarks/results/dev-089-schema13-2026-08-15.md`](../benchmarks/results/dev-089-schema13-2026-08-15.md).

