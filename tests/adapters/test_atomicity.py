from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared_mind.adapters import AdapterFailure, run_import
from shared_mind.service import WorkspaceService
from shared_mind.workspace import Workspace

from tests.adapters.support import RecordingAdapter, canonical_store_snapshot


class _BeforeCommitFailureService(WorkspaceService):
    def commit_proposal(self, proposal: Any):  # type: ignore[no-untyped-def]
        raise AdapterFailure(
            "ADAPTER_BEFORE_COMMIT_FAULT", stage="COMMIT", retryable=False
        )


class _MidCommitFaultService(WorkspaceService):
    def commit_proposal(self, proposal: Any):  # type: ignore[no-untyped-def]
        kernel = self.workspace.open_kernel()
        original = kernel._apply_operation

        def apply_then_fail(operation: Any, events: Any, conflicts: Any) -> None:
            original(operation, events, conflicts)
            raise RuntimeError("injected mid-commit fault")

        kernel._apply_operation = apply_then_fail  # type: ignore[method-assign]
        try:
            kernel.commit(proposal)
        finally:
            kernel.close()


class _LostCommitResponseOnceService(WorkspaceService):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(workspace)
        self.calls = 0

    def commit_proposal(self, proposal: Any):  # type: ignore[no-untyped-def]
        self.calls += 1
        result = super().commit_proposal(proposal)
        if self.calls == 1:
            raise AdapterFailure(
                "ADAPTER_TIMEOUT", stage="COMMIT", retryable=True
            )
        return result


class AdapterAtomicFailureConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace.initialize(Path(self.temp.name) / "workspace")
        self.service = WorkspaceService(self.workspace)
        self.before = canonical_store_snapshot(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_canonical_store_unchanged(self) -> None:
        self.assertEqual(self.before, canonical_store_snapshot(self.workspace))

    def test_source_read_failure_changes_no_canonical_surface(self) -> None:
        adapter = RecordingAdapter(
            failure_stage="SNAPSHOT", failure_code="ADAPTER_SOURCE_READ_FAILED"
        )

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_SOURCE_READ_FAILED", result.code)
        self.assert_canonical_store_unchanged()

    def test_partial_stream_changes_no_canonical_surface(self) -> None:
        adapter = RecordingAdapter(
            failure_stage="SNAPSHOT", failure_code="ADAPTER_PARTIAL_STREAM"
        )

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_PARTIAL_STREAM", result.code)
        self.assert_canonical_store_unchanged()

    def test_nth_transform_failure_changes_no_canonical_surface(self) -> None:
        class NthTransformFailureAdapter(RecordingAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.transformed = 0

            def plan(self, snapshot: Any, mapping: Any | None) -> dict[str, Any]:
                self._record("PLAN", snapshot, mapping)
                for index in range(5):
                    if index == 2:
                        raise AdapterFailure(
                            "ADAPTER_TRANSFORM_FAILED",
                            stage="PLAN",
                            retryable=False,
                        )
                    self.transformed += 1
                raise AssertionError("the injected Nth transform fault did not run")

        adapter = NthTransformFailureAdapter()

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_TRANSFORM_FAILED", result.code)
        self.assertEqual(2, adapter.transformed)
        self.assert_canonical_store_unchanged()

    def test_selector_build_failure_changes_no_canonical_surface(self) -> None:
        adapter = RecordingAdapter(
            failure_stage="PLAN", failure_code="ADAPTER_SELECTOR_BUILD_FAILED"
        )

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_SELECTOR_BUILD_FAILED", result.code)
        self.assert_canonical_store_unchanged()

    def test_after_draft_before_commit_failure_changes_no_canonical_surface(self) -> None:
        adapter = RecordingAdapter()

        result = run_import(
            adapter,
            _BeforeCommitFailureService(self.workspace),
        )

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_BEFORE_COMMIT_FAULT", result.code)
        self.assert_canonical_store_unchanged()

    def test_proposal_validation_rejection_changes_no_canonical_surface(self) -> None:
        class InvalidPlanAdapter(RecordingAdapter):
            def plan(self, snapshot: Any, mapping: Any | None) -> dict[str, Any]:
                self._record("PLAN", snapshot, mapping)
                return {"object_type": "PROPOSAL", "operations": []}

        result = run_import(InvalidPlanAdapter(), self.service)

        self.assertFalse(result.ok)
        self.assertEqual("PROPOSAL_INVALID", result.code)
        self.assert_canonical_store_unchanged()

    def test_mid_commit_fault_rolls_back_ledger_state_receipt_and_projection(self) -> None:
        adapter = RecordingAdapter()

        result = run_import(adapter, _MidCommitFaultService(self.workspace))

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_COMMIT_FAILED", result.code)
        self.assert_canonical_store_unchanged()

    def test_retryable_timeout_before_commit_retries_from_probe_and_commits_once(self) -> None:
        adapter = RecordingAdapter(
            failure_stage="PROBE",
            failure_code="ADAPTER_TIMEOUT",
            retryable=True,
            fail_count=1,
        )

        result = run_import(adapter, self.service, max_attempts=2)

        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual("COMMITTED", result.code)
        self.assertEqual(2, result.data["adapter"]["attempts"])
        after = canonical_store_snapshot(self.workspace)
        self.assertEqual(1, after["ledger"])
        self.assertEqual(1, after["receipts"])
        self.assertEqual(1, after["sources"])
        self.assertTrue(after["verification"]["valid"])

    def test_lost_commit_response_retry_is_idempotent_and_keeps_one_receipt(self) -> None:
        adapter = RecordingAdapter()
        service = _LostCommitResponseOnceService(self.workspace)

        result = run_import(adapter, service, max_attempts=2)

        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(2, service.calls)
        self.assertEqual(2, result.data["adapter"]["attempts"])
        after = canonical_store_snapshot(self.workspace)
        self.assertEqual(1, after["ledger"])
        self.assertEqual(1, after["receipts"])
        self.assertEqual(1, after["sources"])
        self.assertTrue(after["verification"]["valid"])

    def test_retry_exhaustion_changes_no_canonical_surface(self) -> None:
        adapter = RecordingAdapter(
            failure_stage="SNAPSHOT",
            failure_code="ADAPTER_TIMEOUT",
            retryable=True,
            fail_count=3,
        )

        result = run_import(adapter, self.service, max_attempts=2)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_TIMEOUT", result.code)
        self.assertEqual(2, result.data["attempts"])
        self.assert_canonical_store_unchanged()


if __name__ == "__main__":
    unittest.main()
