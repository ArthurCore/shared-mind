from __future__ import annotations

import io
import json
import unittest

from shared_mind.continuity_eval import (
    ContinuityEvaluationError,
    benchmark_context_quality,
    classify_memory_lifecycle,
    evaluate_conflict_resolution,
    evaluate_memory_pollution,
    evaluate_zero_relearning,
)
from shared_mind.product_cli import main as product_cli_main
from shared_mind.product_mcp_server import ProductMcpApplication, TOOL_NAMES

from tests.product_support import ProductTestCase


PURPOSE = "Preserve project state so every new session can continue it."
DECISION_IDS = ["decision-one-state", "decision-no-loadout", "decision-core-view"]
QUESTION_IDS = ["question-wrong-memory"]
CONFLICT_IDS = ["conflict-engine"]
WORK_ITEM_ID = "workitem-dev-082"
SOURCE_IDS = ["revision-bootstrap", "revision-architecture"]


def expectation() -> dict:
    return {
        "expectation_version": "zero-relearning-expectation@1",
        "purpose": PURPOSE,
        "decision_ids": DECISION_IDS,
        "open_question_ids": QUESTION_IDS,
        "conflict_ids": CONFLICT_IDS,
        "active_work_item_id": WORK_ITEM_ID,
        "evidence_source_revision_ids": SOURCE_IDS,
        "critical_memory_ids": [
            *DECISION_IDS,
            *QUESTION_IDS,
            *CONFLICT_IDS,
            WORK_ITEM_ID,
            *SOURCE_IDS,
        ],
        "memory_truth": {
            "one-shared-state": "one canonical state; task-specific views",
            "agent-loadout": "removed because it fragments project memory",
            "core-context": "derived and non-authoritative",
            "project-boundary": "kernel Proposal and append-only ledger",
            "skill-boundary": "versioned ProductMutationProposal",
        },
        "thresholds": {
            "continuity_accuracy": 1.0,
            "decision_recall": 1.0,
            "open_question_recall": 1.0,
            "conflict_recall": 1.0,
            "evidence_traceability": 1.0,
            "wrong_memory_rate": 0.0,
            "missing_critical_memory_rate": 0.0,
            "irrelevant_context_rate": 0.0,
            "max_context_bytes": 16_384,
            "max_context_tokens": 4_096,
            "max_time_to_productive_action_ms": 5_000,
        },
    }


def observation() -> dict:
    return {
        "observation_version": "zero-relearning-observation@1",
        "purpose": PURPOSE,
        "decision_ids": DECISION_IDS,
        "open_question_ids": QUESTION_IDS,
        "conflict_ids": CONFLICT_IDS,
        "active_work_item_id": WORK_ITEM_ID,
        "evidence_source_revision_ids": SOURCE_IDS,
        "memory_assertions": [
            {"memory_id": key, "value": value, "confidence": 1.0}
            for key, value in expectation()["memory_truth"].items()
        ],
    }


def context() -> dict:
    included = expectation()["critical_memory_ids"]
    return {
        "context_version": "shared-task-context@1",
        "context_hash": "sha256:" + "1" * 64,
        "kernel_state_root": "sha256:" + "2" * 64,
        "selection_trace": [
            {"id": item, "kind": "record", "included": True, "reasons": ["fixture"]}
            for item in included
        ],
        "budget": {"included_bytes": 8_192, "budget_bytes": 16_384, "omitted": 0},
        "core_context": {"purpose": PURPOSE},
        "task_context": {},
    }


class ContinuityEvaluationUnitTest(unittest.TestCase):
    def test_dev_082_perfect_zero_relearning_report_has_all_required_metrics(self) -> None:
        report = evaluate_zero_relearning(
            context(), observation(), expectation(), elapsed_ms=1_250, token_count=2_048
        )
        self.assertEqual("zero-relearning-eval@1", report["report_version"])
        self.assertTrue(report["passed"])
        self.assertEqual([], report["failures"])
        self.assertEqual(
            {
                "continuity_accuracy",
                "decision_recall",
                "open_question_recall",
                "conflict_recall",
                "evidence_traceability",
                "wrong_memory_rate",
                "missing_critical_memory_rate",
                "irrelevant_context_rate",
                "context_bytes",
                "context_tokens",
                "time_to_productive_action_ms",
            },
            set(report["metrics"]),
        )
        for name in (
            "continuity_accuracy",
            "decision_recall",
            "open_question_recall",
            "conflict_recall",
            "evidence_traceability",
        ):
            self.assertEqual(1.0, report["metrics"][name])

    def test_dev_082_wrong_missing_and_irrelevant_memory_fail_closed(self) -> None:
        actual = observation()
        actual["decision_ids"] = DECISION_IDS[:-1]
        actual["open_question_ids"] = []
        actual["conflict_ids"] = []
        actual["evidence_source_revision_ids"] = SOURCE_IDS[:1]
        actual["memory_assertions"][0]["value"] = "each agent owns a private state"
        polluted_context = context()
        polluted_context["selection_trace"].append(
            {"id": "irrelevant-memory", "kind": "record", "included": True, "reasons": []}
        )
        report = evaluate_zero_relearning(
            polluted_context, actual, expectation(), elapsed_ms=6_000, token_count=5_000
        )
        self.assertFalse(report["passed"])
        self.assertGreater(report["metrics"]["wrong_memory_rate"], 0)
        self.assertGreater(report["metrics"]["missing_critical_memory_rate"], 0)
        self.assertGreater(report["metrics"]["irrelevant_context_rate"], 0)
        self.assertIn("decision-core-view", report["missing_critical_ids"])
        self.assertIn("one-shared-state", report["wrong_memory_ids"])

    def test_dev_083_pollution_metrics_distinguish_each_failure_mode(self) -> None:
        memories = [
            {"memory_id": "m-current", "semantic_key": "database", "value": "postgresql", "lifecycle": "CURRENT", "relevant": True, "confidence": 1.0},
            {"memory_id": "m-duplicate", "semantic_key": "database", "value": "postgresql", "lifecycle": "CURRENT", "relevant": True, "confidence": 1.0},
            {"memory_id": "m-irrelevant", "semantic_key": "lunch", "value": "pizza", "lifecycle": "CURRENT", "relevant": False, "confidence": 0.8},
            {"memory_id": "m-stale", "semantic_key": "owner", "value": "old-team", "lifecycle": "STALE", "relevant": True, "confidence": 0.7},
            {"memory_id": "m-wrong", "semantic_key": "license", "value": "GPL", "lifecycle": "CURRENT", "relevant": True, "confidence": 0.95},
        ]
        report = evaluate_memory_pollution(
            memories,
            expected_truth={"database": "postgresql", "owner": "arthurcore", "license": "BSD-3-Clause"},
        )
        self.assertEqual("memory-pollution-eval@1", report["report_version"])
        self.assertFalse(report["passed"])
        self.assertEqual(["m-duplicate"], report["duplicate_memory_ids"])
        self.assertEqual(["m-irrelevant"], report["irrelevant_memory_ids"])
        self.assertEqual(["m-stale"], report["stale_memory_ids"])
        self.assertEqual(["m-stale", "m-wrong"], report["wrong_memory_ids"])
        self.assertEqual(["m-wrong"], report["confidently_wrong_memory_ids"])
        self.assertAlmostEqual(2 / 5, report["metrics"]["wrong_memory_rate"])

    def test_dev_084_lifecycle_is_explicit_and_history_is_preserved(self) -> None:
        cases = {
            "ACTIVE": "CURRENT",
            "OPEN": "CURRENT",
            "DOING": "CURRENT",
            "STALE": "STALE",
            "SUPERSEDED": "SUPERSEDED",
            "REVERSED": "SUPERSEDED",
            "DONE": "COMPLETED",
            "ANSWERED": "COMPLETED",
            "RESOLVED": "COMPLETED",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                result = classify_memory_lifecycle({"object_id": status, "status": status})
                self.assertEqual(expected, result["lifecycle"])
                self.assertEqual(expected == "CURRENT", result["eligible_for_current_context"])
                self.assertTrue(result["preserve_history"])

    def test_dev_084_unknown_lifecycle_fails_closed(self) -> None:
        with self.assertRaises(ContinuityEvaluationError) as caught:
            classify_memory_lifecycle({"object_id": "mystery", "status": "MAYBE"})
        self.assertEqual("LIFECYCLE_STATUS_UNSUPPORTED", caught.exception.code)

    def test_dev_085_resolution_preserves_members_claims_and_rationale(self) -> None:
        claims = {
            "claim-a": {"claim_id": "claim-a", "proposition": {"object": "postgresql"}},
            "claim-b": {"claim_id": "claim-b", "proposition": {"object": "mysql"}},
        }
        before = {
            "conflict_id": "conflict-engine",
            "status": "OPEN",
            "episode": 1,
            "version": 1,
            "member_digest": "sha256:" + "3" * 64,
            "members": ["claim-a", "claim-b"],
            "resolution": None,
            "claims": claims,
        }
        resolution = {
            "resolver": {"actor_type": "HUMAN", "actor_id": "human:reviewer"},
            "authority_policy_version": "authority-policy@1",
            "selected_claim_ids": ["claim-a"],
            "rejected_claim_ids": ["claim-b"],
            "rationale": "The signed production runbook supports PostgreSQL.",
            "evidence_link_ids": ["evidence-a"],
            "decided_at": "2026-08-15T00:00:00Z",
            "resolution_epoch": 1,
        }
        after = before | {"status": "RESOLVED", "version": 2, "resolution": resolution}
        report = evaluate_conflict_resolution(before, after)
        self.assertTrue(report["passed"])
        self.assertEqual(1.0, report["metrics"]["original_claim_preservation"])
        self.assertTrue(report["metrics"]["rationale_preserved"])
        self.assertTrue(report["metrics"]["member_partition_complete"])

    def test_dev_085_deleted_claim_or_incomplete_partition_is_rejected(self) -> None:
        before = {
            "conflict_id": "conflict-engine", "status": "OPEN", "episode": 1,
            "version": 1, "member_digest": "sha256:" + "4" * 64,
            "members": ["claim-a", "claim-b"], "resolution": None,
            "claims": {"claim-a": {"v": 1}, "claim-b": {"v": 1}},
        }
        after = before | {
            "status": "RESOLVED", "version": 2,
            "claims": {"claim-a": {"v": 1}},
            "resolution": {
                "resolver": {"actor_type": "HUMAN", "actor_id": "human:reviewer"},
                "selected_claim_ids": ["claim-a"], "rejected_claim_ids": [],
                "rationale": "incomplete", "evidence_link_ids": ["evidence-a"],
                "decided_at": "2026-08-15T00:00:00Z", "resolution_epoch": 1,
            },
        }
        report = evaluate_conflict_resolution(before, after)
        self.assertFalse(report["passed"])
        self.assertIn("ORIGINAL_CLAIM_MISSING", report["failures"])
        self.assertIn("MEMBER_PARTITION_INCOMPLETE", report["failures"])

    def test_dev_086_context_benchmark_enforces_all_thresholds(self) -> None:
        report = benchmark_context_quality(
            context(), observation(), expectation(), elapsed_ms=1_250, token_count=2_048
        )
        self.assertEqual("context-quality-benchmark@1", report["report_version"])
        self.assertTrue(report["passed"])
        self.assertEqual(1.0, report["metrics"]["relevant_recall"])
        self.assertEqual(0.0, report["metrics"]["missing_critical_memory_rate"])
        self.assertEqual(0.0, report["metrics"]["irrelevant_context_rate"])
        self.assertEqual(8_192, report["metrics"]["context_bytes"])
        self.assertEqual(2_048, report["metrics"]["context_tokens"])

    def test_malformed_eval_inputs_fail_with_stable_codes(self) -> None:
        with self.assertRaises(ContinuityEvaluationError) as caught:
            evaluate_zero_relearning({}, observation(), expectation(), elapsed_ms=1)
        self.assertEqual("CONTEXT_INVALID", caught.exception.code)
        with self.assertRaises(ContinuityEvaluationError) as caught:
            evaluate_memory_pollution([], expected_truth={})
        self.assertEqual("POLLUTION_INPUT_EMPTY", caught.exception.code)


class ContinuityEvaluationInterfaceTest(ProductTestCase):
    def _cli(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        code = product_cli_main(
            ["--workspace", str(self.workspace_root), *args], stdout=output
        )
        return code, json.loads(output.getvalue())

    def test_python_cli_and_mcp_share_the_same_zero_relearning_report(self) -> None:
        context_path = self.write_source("eval/context.json", json.dumps(context()))
        observation_path = self.write_source("eval/observation.json", json.dumps(observation()))
        expectation_path = self.write_source("eval/expectation.json", json.dumps(expectation()))
        expected = self.service.evaluate_zero_relearning(
            context(), observation(), expectation(), elapsed_ms=1_250, token_count=2_048
        )

        code, cli = self._cli(
            "metrics", "zero-relearning",
            str(context_path.relative_to(self.workspace_root)),
            str(observation_path.relative_to(self.workspace_root)),
            str(expectation_path.relative_to(self.workspace_root)),
            "--elapsed-ms", "1250", "--token-count", "2048",
        )
        self.assertEqual(0, code)
        self.assertEqual("ZERO_RELEARNING_EVALUATED", cli["code"])
        self.assertEqual(expected, cli["data"])

        app = ProductMcpApplication(self.workspace)
        try:
            self.assertIn("continuity_evaluate", TOOL_NAMES)
            mcp = app.call_tool(
                "continuity_evaluate",
                {
                    "evaluation": "ZERO_RELEARNING",
                    "context": context(),
                    "observation": observation(),
                    "expectation": expectation(),
                    "elapsed_ms": 1_250,
                    "token_count": 2_048,
                },
            )
        finally:
            app.close()
        self.assertFalse(mcp["isError"])
        self.assertEqual(expected, mcp["structuredContent"]["data"])


if __name__ == "__main__":
    unittest.main()
