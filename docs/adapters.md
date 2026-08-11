# External source adapters

Shared Mind provides deterministic, core-outside import adapters for three
pinned external formats. They preserve a captured upstream document as an
immutable `SOURCE_REVISION`; they do not claim that imported text is true and
do not promote it to Claims, EvidenceLinks, decisions, questions, or work items
by default.

This is a local bytes-in integration surface. There is currently no live vendor
network connector, vendor SDK, credential store, login flow, webhook, polling
service, or remote write support. The caller must obtain the complete source
bytes before constructing an adapter. See [Remote adapter policy](remote-policy.md)
for the separate, local-only authorization primitive intended for a future
transport boundary.

## Pinned catalog

The public catalog version is `external-adapter-contract@1`. Every catalog
entry has `source_only_default: true`, allows only `SOURCE_REVISION` output, and
sets semantic promotion to `REVIEWED_MAPPING_ONLY`.

| Adapter | Pinned upstream | Accepted captured format | Current boundary |
|---|---|---|---|
| AtomicStrata | commit `62ef452b92ffd6480140671d5ccd199c6dc4b5aa` | JSON export with citations whose `okf_reference` values resolve in the export's `okf_references` list | Restricted citation import; stores the complete validated export as one source revision. |
| Qarinah | commit `8541db37e0db0373af96fd228f90674272f59979` | Stable event JSON with `event_id`, `event_type`, `created_at`, and object `payload` | Stores the complete validated event as one source revision. |
| SwarmVault | version `3.21.0`, commit pin `815412d24298e59e5073ded1ddd6c0e6aee9b91b` | JSON containing the exact version and a context object with a source list | Provisional source/context import only; graph semantics are not imported. |

The pins are compatibility and review boundaries, not evidence that Shared
Mind can contact those projects. A new upstream version or wider semantic
mapping requires an explicit code, fixture, and conformance-test update.

## Public Python API

`shared_mind.adapters` exports:

- frozen values `AdapterSource`, `AdapterProbe`, `AdapterSnapshot`,
  `AdapterSpec`, `AdapterCatalog`, and `ReviewedMapping`;
- the `ExternalAdapter` protocol and stage-aware `AdapterFailure`;
- `adapter_catalog()`, `create_adapter(name, source)`, and
  `run_import(adapter, service, *, mapping=None, max_attempts=1)`.

`AdapterSource.content` and `AdapterSnapshot.content` must be immutable `bytes`.
The runner recalculates the snapshot's SHA-256 content hash before planning.
An `AdapterSource.locator` is an opaque provenance URI, not a filesystem read
capability. The adapter is never given a `Workspace`, SQLite connection,
canonical database path, source-root path, or arbitrary file-reading API.

The adapter lifecycle is fixed:

```text
probe -> immutable snapshot -> validate -> plan twice -> policy check
      -> WorkspaceService.validate_proposal -> WorkspaceService.commit_proposal
```

The same snapshot is planned twice and both Proposal values are compared using
canonical JSON. A difference fails with `ADAPTER_NONDETERMINISTIC_PLAN` before
canonical mutation. The runner also rejects a snapshot hash mismatch and a
Proposal containing more than 128 operations.

## Exact local example

The caller performs file access. `create_adapter` receives only immutable bytes
and an opaque provenance locator; `run_import` receives the public
transport-neutral service.

```python
from pathlib import Path

from shared_mind.adapters import AdapterSource, create_adapter, run_import
from shared_mind.service import WorkspaceService
from shared_mind.workspace import Workspace

workspace = Workspace.open(".")
payload = Path("exports/qarinah-event.json").read_bytes()
source = AdapterSource(
    locator="urn:qarinah:event:evt-0001",
    media_type="application/json",
    content=payload,
)
adapter = create_adapter("qarinah", source)
result = run_import(adapter, WorkspaceService(workspace), max_attempts=1)

if not result.ok:
    raise RuntimeError(result.as_dict())
print(result.as_dict())
```

The built-in adapters always plan a single `REGISTER_SOURCE_REVISION`
operation. Its revision ID, Proposal ID, idempotency key, content hash, actor,
and timestamp are deterministic for the adapter name, pinned upstream version,
and captured bytes.

## Reviewed semantic mapping

Source-only is the fail-closed default. A custom `ExternalAdapter` that plans
an operation such as `ASSERT_CLAIM` is rejected with
`ADAPTER_REVIEWED_MAPPING_REQUIRED` unless the caller supplies an immutable
`ReviewedMapping`:

```python
from shared_mind.adapters import ReviewedMapping

mapping = ReviewedMapping(
    mapping_id="mapping_atomicstrata_claims_001",
    mapping_version="1.0.0",
    reviewed_by="human:maintainer",
    reviewed_at="2026-08-11T00:00:00Z",
    allowed_operations=("REGISTER_SOURCE_REVISION", "ASSERT_CLAIM"),
)
```

This value is only a narrow operation allowlist at the adapter boundary. It
does not make text true, bypass schema validation, synthesize evidence, weaken
guards, or guarantee that the kernel will accept the Proposal. The mapping
identifier, version, reviewer, allowed operations, transformation rules, and
corresponding fixtures must be reviewed together before a semantic adapter is
enabled. The three built-in adapters ignore semantic mappings and remain
source-only.

## Atomicity, failures, and retries

One adapter run plans one Proposal with at most 128 operations. Nothing is
written during probe, snapshot, adapter validation, deterministic planning, or
`WorkspaceService.validate_proposal`. The only canonical write boundary is the
existing `commit_proposal` call, which retains the kernel's transaction,
receipt, ledger, guard, and replay semantics.

`AdapterFailure` records a stable code, stage, and `retryable` flag. Only an
explicitly retryable `AdapterFailure` is retried, and the complete lifecycle
starts again from `probe`. Other exceptions fail closed; an unexpected commit
exception becomes `ADAPTER_COMMIT_FAILED`. Exhausted attempts return the last
stable adapter code and attempt count.

Deterministic planning also makes a lost commit response safe to retry: the
same Proposal and idempotency key are resubmitted, so the kernel returns the
existing result instead of appending another ledger entry or receipt. Tests
cover source-read failure, partial streams, Nth-transform failure, selector
failure, failure after drafting but before commit, validation rejection,
mid-commit rollback, timeout retry, and lost-response retry. Those tests compare
the ledger, state root, receipts, sources, deterministic projection, and ledger
verification before and after each failure.

For the canonical product invariants and Proposal outcome meanings, see the
[SRS](SRS.md) and [coding-agent bootstrap](agent-bootstrap.md).
