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
| Codex hook-shape RED | Two focused `NaturalLanguageSetupTest` Codex shape/validation tests | 2 intended failures: lifecycle entries were written at document root and a non-object `hooks` field was accepted. |
| Codex hook-shape GREEN | Same two-test command | 2/2 PASS; official nested shape and fail-closed validation are enforced. |

RED checkpoint commit:
`e2585a5 test: add RED gate for project-scoped auto resume`.

Hardening RED checkpoint commit:
`3d6b970 test: harden DEV-105 project session contracts`.

Mixed-hook RED checkpoint commit:
`90866b9 test: preserve unrelated mixed lifecycle hooks`.

Hardening GREEN checkpoint commit:
`e63f8c7 fix: harden project-scoped automatic session restore`.

Codex hook-shape RED checkpoint commit:
`36e560c test: require official Codex hooks document shape`.

Codex hook-shape GREEN checkpoint commit:
`0594620 fix: emit official Codex hooks document`.

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
| 19 | Codex lifecycle arrays are nested under top-level `hooks`, top-level metadata and unrelated mixed hooks survive reconciliation, and invalid `hooks` types fail before partial installation | `NaturalLanguageSetupTest.test_setup_does_not_touch_claude_settings_without_install_hooks`; `test_setup_rejects_non_mapping_codex_hooks_without_partial_install` | PASS |

## Final required gates

| Gate | Result |
|---|---|
| `python3 contracts/validate_contract.py` | PASS: 7 predicates, 16 typed fixtures, 6 negative cases, 6 semantic cases, 7 continuity operations |
| `python3 contracts/validate_product_contract.py` | PASS: 10 typed fixtures, 14 negative cases |
| Focused DEV-105 + manual resume/DEV-102/release regression (8 modules) | 72/72 PASS |
| `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 578 PASS, 0 failures/errors, 1 optional skip |
| `.venv/bin/ruff check` on changed Python/tests and `python3 -m compileall` on changed modules | PASS |

## Installed-entrypoint dogfooding

The editable package was reinstalled with the `mcp` extra so the portable
`shared-mind-session-hook` console entrypoint was exercised exactly as generated
by setup. Because the already-installed global skill differed from the packaged
skill, setup was rerun with `--no-install-skill --install-hooks`; no global skill
file was overwritten.

| Check | Actual result |
|---|---|
| Project setup | `SETUP_READY`; generated `.claude/settings.json`, `.codex/hooks.json`, and the ignored local binding to `/Users/kkh/IdeaProjects/shared-mind-memory` |
| Claude/Codex SessionStart parity | Both returned 24,867 identical bytes; byte SHA-256 `sha256:8d5752164c81e40d88cb26da04bc9fc05db682a8d7ca0eaf20b0438412f67589`; embedded context hash `sha256:7033b75dd34660490ee844eae3fb480fc1aef88ae31fff6820f3591db290c3b7` |
| Second-project isolation | A temporary Git project beside a marker-bearing `fixture-memory` returned `PROJECT_BINDING_NOT_FOUND`, emitted no `hookSpecificOutput`, and did not expose the marker |
| Project-bound capture | One Claude adapter append/finalize cycle increased the bound workspace batch count from 23 to 24 and produced captured file `53972f83e9253171747b50587aee2802.jsonl` only under the bound workspace |

These checks invoke the same installed commands that the hosts launch, without
using manual `shared-mind resume`. A newly launched Claude/Codex model process
is still an operator check because it may require Codex project-hook trust and
would consume an external model request.

## Coverage and exclusions

The repository gate is the full unittest discovery suite. This DEV did not run
a separate coverage command. Cross-project aggregation, global memory search,
automatic workspace creation during SessionStart, and unmodifiable web-chat
injection remain explicit non-goals. Installed hook command delivery is proven
above. Fresh Claude Code/Codex model launches are not claimed here; the operator
must approve Codex project hooks and start new processes to exercise host trust
and paid/external lifecycle delivery.
