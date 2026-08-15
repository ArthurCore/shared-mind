# DEV-098 evaluator policy integrity — TDD evidence

## RED

Seven test methods were added before implementation and produced **19
failures**. Empty/secret-bearing/network-enabled execution policies,
missing/extra/duplicate/unknown adversarial records, malformed candidate
responses, scenario-ID drift, and ineffective or mismatched penalty vectors
were all ignored.

The decisive executable probe replaced the policy with an `api_key` and enabled
network access, then replaced adversarial cases with a raw private prompt. The
scorer still returned 100/100 and `passed=true`.

## GREEN

The scorer validates the exact offline policy and executes every declared
adversarial case through the same closed response contract and fact/conflict
penalty computation used by ordinary reports.

```text
DEV-098 focused: 7/7 PASS
evaluation regression: 51/51 PASS
```

## Full regression

Python 3.13.2 parallel branch coverage completed with:

```text
TOTAL files=61 tests=490 failures=0 seconds=20.753
branch-enabled coverage total 83%
```

Both contract validators, compileall, Ruff, configured mypy, Bandit,
dependency audit, and `git diff --check` passed.

## Self-dogfooding

Canonical Proposals created
`workitem_dev_098_evaluator_policy_integrity_001` and moved it from `TODO`
version 1 to `DOING` version 2 at ledger sequences 196 and 197.

The golden response remained 100/100 and both checked-in adversarial vectors
triggered exactly their declared penalty. Five hostile variants failed closed:
secret policy field, enabled test network, empty vector list, nested raw prompt,
and an ineffective vector.

The immutable trace `trace:dev-098-evaluator-policy-integrity-20260815-001`
was captured into the same Shared State as source revision
`revision_315556b859779b5ac7213da92aefcfcf` at ledger sequence 198. A guarded
Proposal moved the WorkItem from `DOING` version 2 to `DONE` version 3 at
ledger sequence 199.

Final local integrity evidence:

```text
kernel state root: sha256:813aa9c6e0dc8dc48cf6b10428c1c617bff996b19cce558f9dc0738b7368cffb
kernel head:       sha256:eb8f296430e975dd5a3ef9eefec13f21110702a64e57b09c2504e5df052b28e5
kernel replay:     valid=true, checked_entries=199
product audit:     sha256:b61e88c0cc5d577ec0c346d0eeb8855a40c62190da6c0487785c4e152fa82384
product verify:    PRODUCT_INTEGRITY_VALID
next context:      sha256:d14cf034b411126fd75d024b81dc3e065a718815d3fef3b5cd0d15e16b0f7ad7
```

PR #17 first source/test/documentation head
`9e4142f93ddacecfdbc1babaee72f8b57a25ab82` passed all eight jobs in hosted
[run 31876421867](https://github.com/ArthurCore/shared-mind/actions/runs/31876421867):
Python 3.11, 3.12, and 3.13 contract/coverage; Linux, macOS, and Windows
determinism; quality/security; and fresh base/MCP wheel smoke.
