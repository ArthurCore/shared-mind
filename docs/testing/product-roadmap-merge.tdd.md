# Product roadmap merge TDD evidence

## Scope

This evidence covers the type-boundary defects found while verifying
`agent/product-roadmap-implementation` before its fast-forward merge to `main`.
The user journey is: a maintainer can merge the product roadmap implementation
without weakening the repository's blocking type, contract, security, or
regression gates.

## RED

The configured product mypy scope reported nine errors across
`memory_views.py`, `product_ingest.py`, and `product.py`. The failures were
caused by reused local names receiving incompatible inferred collection types,
an unannotated heterogeneous ingest batch, and public executor annotations that
did not use the existing `StepExecutor` protocol. The RED checkpoint is commit
`61ffa8a` (`test: reproduce product mypy boundary failures`).

## GREEN

The fix gives each scenario-member set a distinct typed name, annotates the
ingest batch as `dict[str, Any]`, separates list/mapping accumulator names, and
uses the existing `StepExecutor` protocol at both product service boundaries.
No runtime behavior or canonical contract changed.

| Guarantee | Validation | Result |
|---|---|---|
| Kernel and product contracts remain valid | `python3.13 contracts/validate_contract.py` and `python3.13 contracts/validate_product_contract.py` | PASS |
| Product type boundary is clean | configured mypy scope over 12 source files | PASS, 0 issues |
| Product and legacy behavior remain compatible | `PYTHONPATH=src python -m unittest discover -s tests -q` | PASS, 387 tests, 1 skipped |
| Shipped Python remains lint- and compile-clean | `ruff check src contracts tools` and `compileall` | PASS |
| Dependency and source security gates remain clean | strict `pip-audit`, `bandit -ll -s B608`, and `pip check` | PASS |

The repository's hosted Actions jobs for the branch did not receive a runner
(`runner_id=0`, no executed steps), so they were infrastructure failures rather
than test evidence. A fresh `main` workflow run is required after push.
