# Apache-2.0 and current README TDD evidence

Date: 2026-08-21

## Scope and source of truth

The repository owner's request supplied the two user journeys: an operator can
follow the current Shared Mind setup/resume/observation/review workflow from the
README, and a distributor can identify Apache-2.0 consistently in the repository
and Python package. Command syntax was checked against
`src/shared_mind/cli.py`, `src/shared_mind/product_cli.py`,
`src/shared_mind/web_control.py`, and the DEV-102 through DEV-104 contracts before
the README changed.

`LICENSE` is the unmodified raw body published at
<https://www.apache.org/licenses/LICENSE-2.0.txt>. Its checked-in byte length is
11,358 and its SHA-256 is
`cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`.
Project attribution is carried separately in `NOTICE`.

## RED

The acceptance contract was committed before README, license, NOTICE, or package
metadata implementation changes.

| Stage | Command | Actual result |
|---|---|---|
| RED | `PYTHONPATH=src python3 -m unittest tests.test_package_metadata tests.test_continuity_evaluation -v` | 18 tests ran; 12 intended failures. Existing CLI metadata and all 15 continuity-evaluation tests passed. Failures were confined to absent `NOTICE`, old BSD metadata/license, and missing current README command/route strings. |
| Canonical-layout correction RED | `PYTHONPATH=src python3 -m unittest tests.test_package_metadata.PackageMetadataTest.test_distribution_uses_the_canonical_apache_2_license -v` | 1 intended failure at absent `NOTICE`; the test's raw-file marker was aligned with the official leading whitespace while retaining the exact SHA-256 requirement. |

RED checkpoints:

- `a95caea` — Apache-2.0/package/README acceptance contract and Apache fixture;
- `3d58e1c` — official raw Apache file-layout marker correction.

## GREEN implementation

- replaced the BSD text with the exact Apache License 2.0 body;
- added `NOTICE` with `Shared Mind` and `Copyright 2026 ArthurCore`;
- declared PEP 639 `license = "Apache-2.0"` and
  `license-files = ["LICENSE", "NOTICE"]`;
- documented source-checkout installation, idempotent setup, optional Claude Code
  hooks, resume budgets, explicit observation lifecycle/prune, loopback web launch,
  `/observations`, `/review`, CSRF, and equivalent CLI Draft review;
- retained the earlier BSD evidence as history and marked it superseded.

Focused GREEN:

```text
PYTHONPATH=src python3 -m unittest tests.test_package_metadata tests.test_continuity_evaluation -v
Ran 18 tests in 0.517s
OK
```

## Test specification

| Guarantee | Evidence | Result |
|---|---|---|
| PEP 639 metadata declares `Apache-2.0` and packages LICENSE plus NOTICE | `PackageMetadataTest.test_distribution_uses_the_canonical_apache_2_license` | PASS |
| LICENSE bytes exactly match the official Apache 2.0 file | Same test, pinned SHA-256 and canonical markers | PASS |
| ArthurCore's 2026 attribution remains in NOTICE | Same test | PASS |
| README links both Apache-2.0 and NOTICE | Same test | PASS |
| README covers setup hooks, resume, observation start/append/finalize/prune, loopback web, `/observations`, `/review`, CSRF, and CLI commit/reject | `PackageMetadataTest.test_readme_documents_the_current_operator_workflow` | PASS |
| The continuity-evaluation license fixture follows current project metadata | `ContinuityEvaluationUnitTest.test_dev_083_pollution_metrics_distinguish_each_failure_mode` | PASS |

## Final verification

| Gate | Actual result |
|---|---|
| `python3 contracts/validate_contract.py` | PASS: 7 predicates, 16 typed fixtures, 6 negative cases, 6 semantic cases, 7 continuity operations |
| `python3 contracts/validate_product_contract.py` | PASS: 10 typed fixtures, 14 negative cases |
| `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 554 tests PASS; 1 pre-existing optional MCP SDK skip |
| `git diff --check` | PASS |

An isolated temporary source copy was built without dependency installation or
network access using `python -m build --no-isolation`. Both
`shared_mind_kernel-0.3.0.tar.gz` and
`shared_mind_kernel-0.3.0-py3-none-any.whl` built successfully. Wheel inspection
showed `License-Expression: Apache-2.0` and both
`dist-info/licenses/LICENSE` and `dist-info/licenses/NOTICE`; the sdist also
contained both root files.

## Assumption and legal boundary

The implementation treats the repository owner's explicit instruction as
authorization to change the repository's declared license. Relicensing any
copyrightable contribution owned by a third party can require that contributor's
authorization; this TDD run does not determine contributor ownership or provide
legal advice.
