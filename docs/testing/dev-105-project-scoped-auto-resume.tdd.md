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

RED checkpoint commit:
`e2585a5 test: add RED gate for project-scoped auto resume`.

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

## Final required gates

Pending final run:

```console
python3 contracts/validate_contract.py
python3 contracts/validate_product_contract.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Coverage and exclusions

The repository gate is the full unittest discovery suite. This DEV does not add
a separate coverage command. Cross-project aggregation, global memory search,
automatic workspace creation during SessionStart, and unmodifiable web-chat
injection remain explicit non-goals.
