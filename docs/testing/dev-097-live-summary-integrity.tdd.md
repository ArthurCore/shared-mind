# DEV-097 live summary contract integrity — TDD evidence

## RED

Seven test methods were added before the final implementation. The initial
contract run produced **31 failures and 5 errors**: incomplete provenance,
unknown/private fields, malformed pins and attestation, nested prompt content,
invalid schema status, and forged comparison content were accepted. Non-object
and missing-arm inputs leaked raw `TypeError`/`KeyError` exceptions.

The decisive executable probe supplied only `arms`, a secret `api_key`, a
malformed provider, and a forged comparison. The public helper returned a
passing comparison.

## GREEN

The scorer now caches a Draft 2020-12 validator derived from the checked-in
live-summary schema. Only `comparison` is optional before computation. Existing
metric/report validation runs first so its stable reason codes remain intact;
the complete structural/privacy boundary is enforced before any result returns.

```text
DEV-097 focused: 7/7 PASS
live evaluation regression: 36/36 PASS
```

## Full regression

Python 3.13.2 parallel branch coverage completed with:

```text
TOTAL files=60 tests=483 failures=0 seconds=20.580
branch-enabled coverage total 83%
```

Both contract validators, compileall, Ruff, configured mypy, Bandit,
dependency audit, and `git diff --check` passed.

## Self-dogfooding

Canonical Proposals created
`workitem_dev_097_live_summary_integrity_001` and moved it from `TODO` version
1 to `DOING` version 2 at ledger sequences 192 and 193.

The checked-in Claude artifact reproduced its historical comparison `@1`
exactly and passed current comparison `@2`. Five hostile variants failed closed
with `INVALID_LIVE_SUMMARY`: top-level secret, missing provider, malformed
project digest, nested raw prompt, and forged comparison.

The immutable trace `trace:dev-097-live-summary-integrity-20260815-001` was
captured into the same Shared State as source revision
`revision_2670a7f8df355f223490a03d6e9be453` at ledger sequence 194. A guarded
Proposal moved the WorkItem from `DOING` version 2 to `DONE` version 3 at
ledger sequence 195.

Final local integrity evidence:

```text
kernel state root: sha256:a07207d8a91838c4a9fda7241127e95a123c052ae83f2ff0f19201f61d5da75a
kernel head:       sha256:cacc34e7e00e6fc4d481bfb934f814507c2af551926ead01432d3532c11ac415
kernel replay:     valid=true, checked_entries=195
product audit:     sha256:c7c9eae2ccd434880dbf1346e662dc04307c1439c1ab34073dccc0e33291eefe
product verify:    PRODUCT_INTEGRITY_VALID
next context:      sha256:7cd46433928f941062e3719fe8d1aff067caba251f420a4fb9e56862591aba52
```

Hosted CI evidence is appended after the PR head is published.
