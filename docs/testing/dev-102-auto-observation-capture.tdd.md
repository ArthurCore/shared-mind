# DEV-102 Automatic Observation Capture TDD evidence

## Source and interpretation

The source plan is
[`../DEV-102-104-auto-observation-capture-plan.md`](../DEV-102-104-auto-observation-capture-plan.md).
Only DEV-102 was implemented. DEV-103 and DEV-104 were not started.

The plan's opaque session ID was interpreted as a locator, not a memory partition.
Already-valid semantic session IDs remain exact; other opaque inputs are mapped to a
stable hash-derived semantic ID. Trace start/end timestamps come exclusively from the
first and last supplied event. No current time is introduced into the trace.

The hook failure-record location is the resolved workspace's
`observations/failed/`, or the current project directory's equivalent when workspace
discovery itself fails. Failure records are non-canonical diagnostics.

Independent review resolved two plan ambiguities as required behavior. A
`PostToolUse` event lazily starts capture. It uses a valid input `task_id`, otherwise
the stable Agent-neutral `observation-<session-sha256-prefix>` task ID; a manual
`observe start` remains authoritative for an existing buffer. Captured buffers have
unlimited retention unless the user explicitly calls `observe prune --before`.

## RED and focused GREEN

| Stage | Command | Actual result |
|---|---|---|
| RED | `PYTHONPATH=src python3 -m unittest -v tests.test_observe tests.test_claude_code_hooks tests.test_natural_language_setup` | 17 tests ran: 8 existing tests passed and 9 reports failed at the intended missing surfaces (`shared_mind.observe`, `claude_code_hooks`, `--install-hooks`, `claude_hooks`, and Codex finalize guidance). No syntax, fixture, or dependency failure. |
| Public CLI RED | `PYTHONPATH=src python3 -m unittest -v tests.test_observe.ObservationCaptureTest.test_start_is_idempotent_and_creates_exactly_one_pending_buffer` | 1/1 expected failure: `observe` was not a parser choice. |
| Focused GREEN | `PYTHONPATH=src python3 -m unittest -v tests.test_observe tests.test_claude_code_hooks tests.test_natural_language_setup` | 17/17 PASS. |
| Skill validation | `python3 /Users/kkh/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/shared-mind-setup` | PASS: `Skill is valid!` |
| Review RED | `PYTHONPATH=src python3 -m unittest -v tests.test_claude_code_hooks.ClaudeCodeHooksTest.test_post_tool_use_lazily_starts_with_payload_or_session_task tests.test_observe.ObservationCaptureTest.test_prune_removes_only_captured_buffers_strictly_before_cutoff tests.test_observe.ObservationCaptureTest.test_prune_rejects_non_rfc3339_cutoff_without_file_changes` | 3 tests ran with 4 intended failures: both lazy-start cases found no pending buffer, and `prune` was not a public parser choice. |
| Review focused GREEN | Same three-test command | 3/3 PASS. |

RED checkpoint commits were preserved before production edits:

- `2ed18a5` — all eight acceptance tests and packaged-skill assertion;
- `2f32f87` — public observe parser/dispatch assertion and explicit CLI RED.
- `d66a6a0` — independent-review lazy-start and prune RED checkpoint.

## Acceptance specification

| # | What is guaranteed | Test | Result |
|---|---|---|---|
| 1 | Repeated public CLI `observe start` creates one identical pending buffer | `ObservationCaptureTest.test_start_is_idempotent_and_creates_exactly_one_pending_buffer` | PASS |
| 2 | Six event types finalize through DEV-081 and a fresh service restores identical source events | `ObservationCaptureTest.test_six_event_types_finalize_through_dev_081_and_restore_fresh` | PASS |
| 3 | Re-finalize returns `UNCHANGED` without ledger or audit growth | `ObservationCaptureTest.test_repeated_finalize_is_unchanged_without_ledger_or_audit_growth` | PASS |
| 4 | Invalid append leaves pending bytes unchanged | `ObservationCaptureTest.test_invalid_append_does_not_change_pending_buffer_bytes` | PASS |
| 5 | Missing-workspace hook returns 0, writes one failure record, and creates no canonical database | `ClaudeCodeHooksTest.test_missing_workspace_is_fail_open_and_records_failure_without_mutation` | PASS |
| 6 | Post-registration failure preserves pending bytes and retry reuses the same batch/revision | `ObservationCaptureTest.test_registration_failure_preserves_buffer_and_retry_reuses_source` | PASS |
| 7 | Hook-adapted event order and timestamps remain exact; start/end use supplied event times | `ClaudeCodeHooksTest.test_adapter_preserves_input_event_order_and_timestamps` | PASS |
| 8 | Default setup leaves Claude settings byte-identical; opt-in preserves existing settings and installs three hooks | `NaturalLanguageSetupTest.test_setup_does_not_touch_claude_settings_without_install_hooks` | PASS |
| 9 | A standard PostToolUse payload lazily starts with its valid task or a deterministic session-derived fallback, while manual metadata is preserved | `ClaudeCodeHooksTest.test_post_tool_use_lazily_starts_with_payload_or_session_task`; existing adapter preservation test | PASS |
| 10 | Prune deletes only captured buffers ending strictly before cutoff and changes no canonical/product state | `ObservationCaptureTest.test_prune_removes_only_captured_buffers_strictly_before_cutoff` | PASS |
| 11 | A non-RFC3339 cutoff is rejected before any buffer change | `ObservationCaptureTest.test_prune_rejects_non_rfc3339_cutoff_without_file_changes` | PASS |

## Final required gates

The final validator and regression results are recorded after the commands run; no
result is inferred from the focused suite.

| Gate | Actual result |
|---|---|
| `python3 contracts/validate_contract.py` | PASS: 7 predicates, 16 typed fixtures, 6 negative cases, 6 semantic cases, and 7 continuity operations. |
| `python3 contracts/validate_product_contract.py` | PASS: 10 typed fixtures and 14 negative cases. |
| `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 539 tests, 0 failures/errors; 1 pre-existing optional MCP SDK v1 skip. |

## Coverage and known gaps

The repository's required DEV gate is the complete unittest discovery command above;
no separate coverage command was required by the DEV-102 plan. The scope deliberately
does not add web streaming, review promotion, model extraction, automatic summary
injection, or an Agent-specific state table. Actual Claude hook payloads must
carry an original timestamp (or a complete task-trace event); timestamp-free payloads
fail open rather than inventing canonical time.
