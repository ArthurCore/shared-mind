# DEV-104 — Review-queue Promotion UX

> **Project has state. Agents come and go.**
>
> **관찰은 자동, 정본 승격은 검문.**

상태: **DONE (local gates)**

## Review HTTP contract

The loopback-only `shared-mind-web` surface provides:

```text
GET  /api/drafts?state=
GET  /api/drafts/<draft-id>
POST /api/drafts/<draft-id>/commit
POST /api/drafts/<draft-id>/reject
GET  /review
```

`state` is the review-page alias for the existing draft `status` filter; `status`
remains accepted for compatibility. List and detail return the existing DraftProposal
record, including extractor ID/version, model, prompt version, source revision IDs,
input hashes, parameters, and disclosure-policy provenance.

Commit and reject call only `ProductService.commit_draft` and
`ProductService.reject_draft`. The web layer does not construct a second Proposal,
write SQLite/kernel state directly, or create a second promotion receipt. DEV-104
commit/reject request bodies must carry the same explicit `draft_id` selected in the
URL; reject additionally requires a non-empty rationale. The pre-existing edit route
retains its historical `{document, expected_version}` body without redundant ID.

- A kernel-proposal commit runs existing validation, kernel commit, receipt, and Draft
  update behavior. CLI and Web therefore return the same stored Draft/receipt.
- Re-committing a COMMITTED draft returns that stored Draft and adds no kernel ledger,
  receipt, or product-audit event.
- Reject changes only the staged Draft/product audit. Kernel ledger head and state root
  remain unchanged.
- A validation-failing commit remains fail-closed: canonical state does not advance and
  the existing Draft failure receipt/status remains reviewable.
- No automatic approval, approve-all action, or `commit-batch` Web route exists.

## CSRF and loopback boundary

Each `WebControlApplication` creates an unpredictable 32-byte-equivalent URL-safe
token with `secrets.token_urlsafe`. It is ephemeral process/application state: it is
never written to canonical state, ProductStore, audit, telemetry, cookie, or backup.
The served `/review` document receives the token inline.

Every valid POST route in the Web application—including context/search/tool telemetry,
build, Skill actions, and Draft actions—must send it in
`X-Shared-Mind-CSRF-Token`. Validation occurs before body parsing or ProductService
dispatch and uses `hmac.compare_digest`. Missing or wrong tokens return HTTP 403 with
`CSRF_TOKEN_INVALID` and perform no service mutation. The custom header also prevents
a cross-origin simple request; no CORS permission is added.

The existing `_require_loopback` bind restriction is unchanged. Token validation is an
additional browser-request boundary, not permission for a non-loopback listener.

## Dependency-free review page

`/review` is a single HTML document with inline JavaScript and no external library.
The operator chooses a state, selects one Draft, reviews the complete record and
provenance, then explicitly selects commit or reject. Reject prompts for a rationale.
The page sends the selected Draft ID in both URL and JSON body and includes the CSRF
header. There is intentionally no bulk or automatic approval control.

The five promotion acceptance guarantees, CSRF coverage, and UI evidence are recorded
in [`testing/dev-104-review-queue-web.tdd.md`](testing/dev-104-review-queue-web.tdd.md).
