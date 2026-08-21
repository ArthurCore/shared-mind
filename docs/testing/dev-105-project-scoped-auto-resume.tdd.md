# DEV-105 Project-scoped Auto Resume TDD Evidence

## Source plan and scope

Source plan:
[`../DEV-105-project-scoped-auto-resume-plan.md`](../DEV-105-project-scoped-auto-resume-plan.md).

Scope is automatic session context restore selected by cwd -> nearest Git root
-> project binding -> exact Shared Mind workspace. Manual `resume` remains a
recovery/custom-budget path.

## RED and focused GREEN

| Stage | Command | Actual result |
|---|---|---|
| RED | `PYTHONPATH=src python3 -m unittest tests.test_project_session_bootstrap tests.test_session_hook_adapters -v` | 2 import errors for missing `shared_mind.session_bootstrap` and `shared_mind.adapters.session_hooks`; fixtures did not run because implementation modules did not exist. |
| GREEN | `PYTHONPATH=src python3 -m unittest tests.test_project_session_bootstrap tests.test_session_hook_adapters -v` | 7/7 PASS. |
| Setup idempotency RED | `PYTHONPATH=src python3 -m unittest tests.test_natural_language_setup.NaturalLanguageSetupTest.test_setup_does_not_touch_claude_settings_without_install_hooks -v` | 1 intended failure: second `setup --install-hooks` reported project binding `INSTALLED` instead of `UNCHANGED`. |
| Setup idempotency GREEN | Same single-test command | 1/1 PASS. |
| Hardening RED | `PYTHONPATH=src python3 -m unittest tests.test_project_session_bootstrap tests.test_session_hook_adapters tests.test_natural_language_setup tests.test_package_metadata tests.test_release_gates -v` at `3d6b970` | 52 run, 17 intended failures. Failures covered closed binding shape/rebind, neutral capture, strict nested setup, binding-last rollback transaction, portable entrypoint/gitignore, and CI parity. |
| Hardening GREEN | Same five-module command | 52/52 PASS. |
| Mixed-hook preservation RED | `PYTHONPATH=src python3 -m unittest tests.test_natural_language_setup.NaturalLanguageSetupTest.test_setup_does_not_touch_claude_settings_without_install_hooks -v` | 1 intended failure: an unrelated command sharing an entry with a legacy managed hook was removed. |
| Mixed-hook preservation GREEN | Same single-test command | 1/1 PASS; only the legacy managed command is reconciled. |

RED checkpoint commit:
`e2585a5 test: add RED gate for project-scoped auto resume`.

Hardening RED checkpoint commit:
`3d6b970 test: harden DEV-105 project session contracts`.

Mixed-hook RED checkpoint commit:
`90866b9 test: preserve unrelated mixed lifecycle hooks`.

## Acceptance specification

| # | What is guaranteed | Test | Result |
|---|---|---|---|
| 1 | Nested cwd under a Git project resolves the exact project binding and workspace | `ProjectSessionBootstrapTest.test_nested_cwd_uses_exact_project_binding_without_cross_project_fallback` | PASS |
| 2 | Neighboring project memories are not cross-loaded | `ProjectSessionBootstrapTest.test_nested_cwd_uses_exact_project_binding_without_cross_project_fallback` | PASS |
| 3 | A nested Git repository selects its own binding, not the parent binding | `ProjectSessionBootstrapTest.test_nested_git_repository_selects_its_own_binding` | PASS |
| 4 | Missing binding does not fall back to conventional sibling workspace discovery | `ProjectSessionBootstrapTest.test_missing_binding_does_not_load_conventional_sibling_workspace` | PASS |
| 5 | Invalid binding fails closed with no context hash and no additional context | `ProjectSessionBootstrapTest.test_invalid_binding_and_integrity_fail_closed_without_context` | PASS |
| 6 | Claude and Codex SessionStart adapters emit byte-identical additional context | `SessionHookAdaptersTest.test_claude_and_codex_session_start_emit_identical_context` | PASS |
| 7 | UserPromptSubmit uses the prompt within the verified binding | `SessionHookAdaptersTest.test_user_prompt_submit_refines_only_the_verified_project_binding` | PASS |
| 8 | Missing binding hook output carries only a bounded warning and no additional context | `SessionHookAdaptersTest.test_missing_binding_emits_warning_without_additional_context` | PASS |
| 9 | `setup --install-hooks` installs binding, Claude hooks, and Codex hooks idempotently while preserving unrelated settings | `NaturalLanguageSetupTest.test_setup_does_not_touch_claude_settings_without_install_hooks` | PASS |
| 10 | Setup without `--install-hooks` touches none of binding, Claude hooks, or Codex hooks | `NaturalLanguageSetupTest.test_setup_does_not_touch_claude_settings_without_install_hooks` | PASS |
| 11 | Binding schema rejects extra fields, symlink paths, root/config mismatches, and invalid product integrity without state mutation | `ProjectSessionBootstrapTest.test_binding_schema_is_closed_and_rejects_extra_fields_without_mutation`; related mismatch/symlink/integrity tests | PASS |
| 12 | Existing different binding cannot be silently overwritten; explicit setup workspace is the sole rebind authority | `ProjectSessionBootstrapTest.test_write_project_binding_rejects_silent_workspace_rebind`; `NaturalLanguageSetupTest.test_explicit_workspace_is_the_only_rebind_authority` | PASS |
| 13 | Implicit nested-project setup uses only its verified binding or exact sibling, never ancestor memory | `NaturalLanguageSetupTest.test_nested_project_implicit_setup_never_reuses_ancestor_memory`; `test_nested_project_implicit_setup_reuses_only_its_verified_binding` | PASS |
| 14 | Prompt cwd is re-resolved and cannot reuse a prior project's binding | `SessionHookAdaptersTest.test_user_prompt_submit_changed_cwd_does_not_reuse_original_binding` | PASS |
| 15 | Neutral append/finalize ignores stale workspace hints and writes only through the cwd binding | `SessionHookAdaptersTest.test_neutral_append_and_finalize_use_verified_cwd_binding_only` | PASS |
| 16 | Claude/Codex/binding installation publishes binding last and rolls back each injected post-replace failure | `NaturalLanguageSetupTest.test_hook_install_publishes_binding_last`; both `test_hook_install_rollback_*` tests | PASS |
| 17 | Generated lifecycle hooks use one portable entrypoint, preserve unrelated hooks, embed no absolute path, and use a bounded approximate Codex token limit | `NaturalLanguageSetupTest.test_setup_does_not_touch_claude_settings_without_install_hooks` | PASS |
| 18 | The console entrypoint is packaged, the machine-local binding is gitignored, and bootstrap parity runs in the 3-OS determinism subset | `PackageMetadataTest`; `ReleaseGateStructureTest.test_determinism_subset_runs_on_linux_macos_and_windows` | PASS |

## Final required gates

| Gate | Result |
|---|---|
| `python3 contracts/validate_contract.py` | PASS: 7 predicates, 16 typed fixtures, 6 negative cases, 6 semantic cases, 7 continuity operations |
| `python3 contracts/validate_product_contract.py` | PASS: 10 typed fixtures, 14 negative cases |
| Focused DEV-105 + manual resume/DEV-102/release regression (8 modules) | 71/71 PASS |
| `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 577 PASS, 0 failures/errors, 1 optional skip |
| `.venv/bin/ruff check` on changed Python/tests and `python3 -m compileall` on changed modules | PASS |

## Coverage and exclusions

The repository gate is the full unittest discovery suite. This DEV did not run
a separate coverage command. Cross-project aggregation, global memory search,
automatic workspace creation during SessionStart, and unmodifiable web-chat
injection remain explicit non-goals. Fresh Claude Code/Codex host launches are
not claimed here; the parent/operator must run them after installation so host
trust prompts and real lifecycle delivery are exercised.
