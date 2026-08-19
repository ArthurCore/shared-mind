"""Conformance tests for the thin WorkItem write wrapper (DEV-103)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from shared_mind.work_items import (
    WorkItemWriteError,
    handoff,
    list_work_items,
    progress,
)
from shared_mind.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "contracts" / "atlas-predicate-registry.v1.json"

ACTOR = "agent:discord-bot"


class WorkItemWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace.initialize(
            Path(self.temp.name) / "memory",
            registry_source=REGISTRY,
            purpose="DEV-103 wrapper conformance workspace.",
        )

    def test_handoff_creates_todo_work_item_readable_by_list(self) -> None:
        work_item_id = handoff(
            self.workspace,
            "DEV-103: hand a Discord request to the orchestration session.",
            actor=ACTOR,
        )

        items = list_work_items(self.workspace)

        self.assertEqual(1, len(items))
        item = items[0]
        self.assertEqual(work_item_id, item["work_item_id"])
        self.assertEqual("TODO", item["status"])
        self.assertEqual(1, item["version"])
        self.assertIsNone(item["blocker"])
        self.assertEqual("P1", item["priority"])

    def test_handoff_accepts_priority_and_related_objects(self) -> None:
        first = handoff(self.workspace, "First task.", actor=ACTOR)
        second = handoff(
            self.workspace,
            "Second task that continues the first.",
            actor=ACTOR,
            priority="P0",
            related=[{"record_type": "WORK_ITEM", "record_id": first}],
        )

        item = _by_id(list_work_items(self.workspace), second)
        self.assertEqual("P0", item["priority"])
        self.assertEqual(
            [{"record_type": "WORK_ITEM", "record_id": first}],
            item["related_objects"],
        )

    def test_handoff_is_idempotent_for_identical_input(self) -> None:
        first = handoff(self.workspace, "Same request.", actor=ACTOR)
        second = handoff(self.workspace, "Same request.", actor=ACTOR)

        self.assertEqual(first, second)
        self.assertEqual(1, len(list_work_items(self.workspace)))

    def test_todo_to_doing_to_done_transition(self) -> None:
        work_item_id = handoff(self.workspace, "Ship the wrapper.", actor=ACTOR)

        doing = progress(
            self.workspace,
            work_item_id,
            "DOING",
            "Session picked the task up.",
            actor=ACTOR,
        )
        self.assertEqual("COMMITTED", doing["code"])
        self.assertEqual("DOING", doing["status"])
        self.assertEqual(2, doing["version"])

        done = progress(
            self.workspace,
            work_item_id,
            "DONE",
            "Wrapper landed with passing tests.",
            actor=ACTOR,
        )
        self.assertEqual("COMMITTED", done["code"])
        self.assertEqual("DONE", done["status"])
        self.assertEqual(3, done["version"])

        item = _by_id(list_work_items(self.workspace), work_item_id)
        self.assertEqual("DONE", item["status"])
        self.assertIsNone(item["blocker"])

    def test_blocked_requires_blocker(self) -> None:
        work_item_id = handoff(self.workspace, "Blocked task.", actor=ACTOR)

        with self.assertRaises(WorkItemWriteError) as caught:
            progress(
                self.workspace,
                work_item_id,
                "BLOCKED",
                "Waiting on the deploy key.",
                actor=ACTOR,
            )
        self.assertIn("blocker", str(caught.exception).lower())

        result = progress(
            self.workspace,
            work_item_id,
            "BLOCKED",
            "Waiting on the deploy key.",
            actor=ACTOR,
            blocker="Missing deploy credentials.",
        )
        self.assertEqual("COMMITTED", result["code"])
        item = _by_id(list_work_items(self.workspace), work_item_id)
        self.assertEqual("BLOCKED", item["status"])
        self.assertEqual("Missing deploy credentials.", item["blocker"])

    def test_non_blocked_status_rejects_blocker(self) -> None:
        work_item_id = handoff(self.workspace, "Task with stray blocker.", actor=ACTOR)

        with self.assertRaises(WorkItemWriteError) as caught:
            progress(
                self.workspace,
                work_item_id,
                "DOING",
                "Starting work.",
                actor=ACTOR,
                blocker="Should not be accepted.",
            )
        self.assertIn("blocker", str(caught.exception).lower())

    def test_invalid_status_is_rejected(self) -> None:
        work_item_id = handoff(self.workspace, "Task with bad status.", actor=ACTOR)

        with self.assertRaises(WorkItemWriteError):
            progress(
                self.workspace,
                work_item_id,
                "FINISHED",
                "Not a kernel status.",
                actor=ACTOR,
            )

    def test_progress_on_unknown_work_item_raises(self) -> None:
        with self.assertRaises(WorkItemWriteError):
            progress(
                self.workspace,
                "workitem_missing_00000000000000000000",
                "DOING",
                "No such item.",
                actor=ACTOR,
            )

    def test_list_work_items_filters_by_status(self) -> None:
        first = handoff(self.workspace, "Stays TODO.", actor=ACTOR)
        second = handoff(self.workspace, "Moves to DOING.", actor=ACTOR)
        progress(self.workspace, second, "DOING", "Started.", actor=ACTOR)

        todo = list_work_items(self.workspace, status="TODO")
        doing = list_work_items(self.workspace, status="DOING")

        self.assertEqual([first], [item["work_item_id"] for item in todo])
        self.assertEqual([second], [item["work_item_id"] for item in doing])

    def test_competing_writers_produce_one_winner_and_one_rebased_success(self) -> None:
        work_item_id = handoff(self.workspace, "Contended task.", actor=ACTOR)

        # Two independent handles model two sessions. Session B reads version 1,
        # then session A commits inside B's write window, so B's first attempt
        # carries a stale guard and must rebase to succeed.
        writer_a = Workspace.open(self.workspace.root)
        writer_b = Workspace.open(self.workspace.root)
        races = iter(
            [
                lambda: progress(
                    writer_a,
                    work_item_id,
                    "DOING",
                    "Session A started first.",
                    actor="agent:session-a",
                )
            ]
        )

        second = progress(
            writer_b,
            work_item_id,
            "DONE",
            "Session B finished after rebasing onto A.",
            actor="agent:session-b",
            _on_rebase=lambda: next(races, lambda: None)(),
        )

        self.assertEqual("COMMITTED", second["code"])
        self.assertEqual(3, second["version"])
        self.assertEqual("DOING", second["previous_status"])
        self.assertTrue(second["rebased"])

        item = _by_id(list_work_items(self.workspace), work_item_id)
        self.assertEqual("DONE", item["status"])
        self.assertEqual(3, item["version"])

    def test_two_concurrent_writers_both_succeed_without_silent_overwrite(self) -> None:
        work_item_id = handoff(self.workspace, "Threaded contention.", actor=ACTOR)
        start = threading.Barrier(2)

        def write(session: str, status: str) -> dict:
            handle = Workspace.open(self.workspace.root)
            start.wait(timeout=10)
            return progress(
                handle,
                work_item_id,
                status,
                f"{session} transition.",
                actor=f"agent:{session}",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(write, "session-a", "DOING"),
                pool.submit(write, "session-b", "DONE"),
            ]
            results = [future.result() for future in futures]

        # Both land because the loser rebases; neither transition is lost.
        self.assertEqual({"COMMITTED"}, {result["code"] for result in results})
        self.assertEqual({2, 3}, {result["version"] for result in results})
        item = _by_id(list_work_items(self.workspace), work_item_id)
        self.assertEqual(3, item["version"])

    def test_stale_expected_version_retries_once_then_raises(self) -> None:
        work_item_id = handoff(self.workspace, "Repeatedly contended task.", actor=ACTOR)

        # A guard that is stale even after one rebase must surface, never overwrite.
        with self.assertRaises(WorkItemWriteError) as caught:
            progress(
                self.workspace,
                work_item_id,
                "DONE",
                "Should fail after the single retry.",
                actor=ACTOR,
                _on_rebase=lambda: progress(
                    Workspace.open(self.workspace.root),
                    work_item_id,
                    "DOING",
                    "Another session raced in again.",
                    actor="agent:racer",
                ),
            )
        self.assertIn("TRANSACTION_CONFLICT", str(caught.exception))

    def test_versions_are_read_from_the_workspace_not_hardcoded(self) -> None:
        from shared_mind.service import WorkspaceService

        handoff(self.workspace, "Version pin check.", actor=ACTOR)
        proposal = _last_proposal(self.workspace)

        expected = WorkspaceService(self.workspace).current_version_bundle()
        self.assertEqual(expected, proposal["versions"])

    def test_status_update_proposal_carries_reads_and_guards(self) -> None:
        work_item_id = handoff(self.workspace, "Guard shape check.", actor=ACTOR)
        progress(self.workspace, work_item_id, "DOING", "Started.", actor=ACTOR)
        proposal = _last_proposal(self.workspace)

        self.assertEqual(
            [
                {
                    "kind": "AGGREGATE",
                    "aggregate_type": "WORK_ITEM",
                    "aggregate_id": work_item_id,
                    "expected_version": 1,
                }
            ],
            proposal["reads"],
        )
        self.assertEqual(
            [
                {
                    "op": "WORK_ITEM_STATUS_EQ",
                    "work_item_id": work_item_id,
                    "expected_status": "TODO",
                },
                {
                    "op": "WORK_ITEM_VERSION_EQ",
                    "work_item_id": work_item_id,
                    "expected_version": 1,
                },
            ],
            proposal["guards"],
        )

    def test_generated_proposals_satisfy_the_published_schema(self) -> None:
        import json

        from jsonschema import Draft202012Validator, FormatChecker

        schema = json.loads(
            (ROOT / "contracts" / "shared-mind-kernel.schema.v1.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema, format_checker=FormatChecker())

        work_item_id = handoff(self.workspace, "Schema check.", actor=ACTOR)
        validator.validate(_last_proposal(self.workspace))
        progress(self.workspace, work_item_id, "BLOCKED", "Halted.", actor=ACTOR, blocker="Nope.")
        validator.validate(_last_proposal(self.workspace))

    def test_actor_must_be_a_semantic_id(self) -> None:
        with self.assertRaises(WorkItemWriteError):
            handoff(self.workspace, "Bad actor.", actor="Discord Bot")

    def test_description_must_not_be_empty(self) -> None:
        with self.assertRaises(WorkItemWriteError):
            handoff(self.workspace, "   ", actor=ACTOR)


def _by_id(items: list[dict], work_item_id: str) -> dict:
    for item in items:
        if item["work_item_id"] == work_item_id:
            return item
    raise AssertionError(f"work item not found: {work_item_id}")


def _last_proposal(workspace: Workspace) -> dict:
    import json

    kernel = workspace.open_kernel()
    try:
        row = kernel.connection.execute(
            "SELECT proposal FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    finally:
        kernel.close()
    return json.loads(row["proposal"])


if __name__ == "__main__":
    unittest.main()
