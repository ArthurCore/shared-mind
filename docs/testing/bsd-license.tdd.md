# BSD-3-Clause licensing TDD evidence

Date: 2026-08-15

## Scope

The repository owner first requested a proprietary, nonmodifiable license and
then explicitly replaced that requirement with the standard BSD license. The
final accepted behavior is `BSD-3-Clause`, which permits redistribution,
modification, and commercial use subject to its three conditions.

## User journey

As a distributor or user of Shared Mind, I can identify one standard license
from the repository and Python distribution metadata, and the built wheel
contains the same license text.

## RED

Command:

```bash
PYTHONPATH=src python3.13 -m unittest tests.test_package_metadata -v
```

Result after the BSD requirement replaced the earlier proprietary draft:

```text
Ran 2 tests
FAILED (failures=1)
AssertionError: False is not true
```

The intended failure was the absent root `LICENSE` file. The pre-existing CLI
and contract packaging test remained green.

## GREEN

The implementation adds the OSI-standard BSD 3-Clause text, declares
`license = "BSD-3-Clause"` and `license-files = ["LICENSE"]` using PEP 639,
and links the license from the README.

Targeted result:

```text
Ran 2 tests
OK
```

An isolated PEP 517 build and `twine check` passed. Direct wheel inspection
confirmed both:

```text
License-Expression: BSD-3-Clause
shared_mind_kernel-0.3.0.dist-info/licenses/LICENSE
```

## Test specification

| Guarantee | Evidence | Result |
|---|---|---|
| Repository has the canonical BSD 3-Clause text and ArthurCore notice | `tests.test_package_metadata.PackageMetadataTest.test_distribution_uses_the_standard_bsd_3_clause_license` | PASS |
| Python metadata uses the SPDX identifier and packages the license file | Targeted test plus isolated wheel inspection | PASS |
| Existing installed CLI and contract-file metadata remains intact | `tests.test_package_metadata.PackageMetadataTest.test_installed_package_exposes_cli_and_default_contracts` | PASS |

## Important semantic boundary

BSD-3-Clause is permissive. It does not prevent modification, redistribution,
or paid commercial use. It requires preservation of notices and disallows
using the copyright holder or contributor names to endorse derived products
without prior written permission.
