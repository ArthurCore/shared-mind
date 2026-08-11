from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from shared_mind import Kernel
from shared_mind.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]
_BARRIER_CLI = """
import os
import time
from pathlib import Path
from shared_mind.cli import main

ready = Path(os.environ["SHARED_MIND_TEST_READY"])
release = Path(os.environ["SHARED_MIND_TEST_RELEASE"])
ready.touch()
deadline = time.monotonic() + 15
while not release.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("multi-client release barrier timed out")
    time.sleep(0.005)
raise SystemExit(main())
"""


@dataclass(frozen=True)
class ClientResult:
    client_id: str
    pid: int
    exit_code: int
    document: dict[str, Any]


class JsonCommitClient(Protocol):
    """Transport seam that a future MCP dispatcher client can also implement."""

    client_id: str

    def start_commit(
        self,
        workspace: Path,
        proposal_path: Path,
        ready_path: Path,
        release_path: Path,
    ) -> subprocess.Popen[str]: ...


@dataclass(frozen=True)
class CliJsonClient:
    client_id: str

    def start_commit(
        self,
        workspace: Path,
        proposal_path: Path,
        ready_path: Path,
        release_path: Path,
    ) -> subprocess.Popen[str]:
        environment = _python_environment()
        environment["SHARED_MIND_TEST_READY"] = str(ready_path)
        environment["SHARED_MIND_TEST_RELEASE"] = str(release_path)
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                _BARRIER_CLI,
                "--workspace",
                str(workspace),
                "proposal",
                "commit",
                str(proposal_path),
                "--json",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


class MultiClientAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace_root = self.root / "workspace"
        self.workspace = Workspace.initialize(
            self.workspace_root, purpose="Exercise two independent JSON clients."
        )
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text()
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        kernel = self.workspace.open_kernel()
        try:
            receipt = kernel.register_source(
                copy.deepcopy(self.objects["source_revision_postgresql"]),
                (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes(),
            )
        finally:
            kernel.close()
        self.assertEqual("COMMITTED", receipt.outcome)
        self.clients: tuple[JsonCommitClient, JsonCommitClient] = (
            CliJsonClient("cli-agent-a"),
            CliJsonClient("cli-agent-b"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_two_cli_processes_competing_claim_supersedes_have_one_winner(
        self,
    ) -> None:
        self._commit_seed("assert_postgresql_proposal")
        proposals = [self._supersede(index) for index in (1, 2)]
        ledger_before = self._ledger_count()

        results = self._race(proposals)

        self._assert_destructive_race(results, proposals, ledger_before)
        kernel = self.workspace.open_kernel()
        try:
            target = kernel.connection.execute(
                "SELECT status, version, superseded_by FROM claims "
                "WHERE claim_id = 'claim_atlas_postgresql_001'"
            ).fetchone()
            self.assertEqual(("SUPERSEDED", 2), (target["status"], target["version"]))
            self.assertIn(
                target["superseded_by"],
                {
                    "claim_multi_client_replacement_01",
                    "claim_multi_client_replacement_02",
                },
            )
            self.assertEqual(
                1,
                kernel.connection.execute(
                    "SELECT COUNT(*) FROM claims "
                    "WHERE claim_id LIKE 'claim_multi_client_replacement_%' "
                    "AND status = 'ACTIVE'"
                ).fetchone()[0],
            )
        finally:
            kernel.close()
        self._assert_replay_root_parity()

    def test_two_cli_processes_answer_drop_question_have_one_winner(self) -> None:
        for fixture in (
            "assert_postgresql_proposal",
            "record_decision_proposal",
            "open_question_proposal",
        ):
            self._commit_seed(fixture)
        answer = self._as_actor(
            self.objects["answer_question_proposal"], "question-answer", 1
        )
        answer["operations"][0]["answer"]["answer_reference"]["record_id"] = (
            "decision_atlas_database_strategy_001"
        )
        drop = self._as_actor(
            self.objects["drop_question_proposal"], "question-drop", 2
        )
        ledger_before = self._ledger_count()

        results = self._race([answer, drop])

        self._assert_destructive_race(results, [answer, drop], ledger_before)
        kernel = self.workspace.open_kernel()
        try:
            row = kernel.connection.execute(
                "SELECT status, version, answer, drop_record FROM open_questions "
                "WHERE question_id = 'question_atlas_cutover_window_001'"
            ).fetchone()
            self.assertIn(row["status"], {"ANSWERED", "DROPPED"})
            self.assertEqual(2, row["version"])
            populated_resolutions = int(row["answer"] is not None) + int(
                row["drop_record"] is not None
            )
            self.assertEqual(1, populated_resolutions)
        finally:
            kernel.close()
        self._assert_replay_root_parity()

    def test_two_cli_processes_preserve_both_commutative_evidence_attaches(
        self,
    ) -> None:
        self._commit_seed("assert_postgresql_proposal")
        proposals = [self._attach(index, unique_key=True) for index in (1, 2)]
        ledger_before = self._ledger_count()

        results = self._race(proposals)

        self.assertEqual(
            ["COMMITTED", "COMMITTED"],
            sorted(result.document["code"] for result in results),
        )
        self.assertEqual(ledger_before + 2, self._ledger_count())
        self._assert_attempts_auditable(results, proposals)
        kernel = self.workspace.open_kernel()
        try:
            rows = kernel.connection.execute(
                "SELECT evidence_link_id FROM evidence "
                "WHERE evidence_link_id LIKE 'evidence_multi_client_attach_%'"
            ).fetchall()
            self.assertEqual(
                {
                    "evidence_multi_client_attach_01",
                    "evidence_multi_client_attach_02",
                },
                {row["evidence_link_id"] for row in rows},
            )
            version = kernel.connection.execute(
                "SELECT version FROM claims "
                "WHERE claim_id = 'claim_atlas_postgresql_001'"
            ).fetchone()[0]
            self.assertEqual(3, version)
        finally:
            kernel.close()
        self._assert_replay_root_parity()

    def test_two_cli_processes_reusing_one_idempotency_key_record_the_loser(
        self,
    ) -> None:
        self._commit_seed("assert_postgresql_proposal")
        proposals = [self._attach(index, unique_key=False) for index in (1, 2)]
        ledger_before = self._ledger_count()

        results = self._race(proposals)

        self.assertEqual(
            ["COMMITTED", "VALIDATION_ERROR"],
            sorted(result.document["code"] for result in results),
        )
        rejected = next(r for r in results if r.document["code"] == "VALIDATION_ERROR")
        self.assertEqual(3, rejected.exit_code)
        self.assertEqual(
            ["IDEMPOTENCY_KEY_REUSE"], rejected.document["data"]["reason_codes"]
        )
        self.assertEqual(ledger_before + 1, self._ledger_count())
        self._assert_attempts_auditable(results, proposals)
        self._assert_replay_root_parity()

    def _race(self, proposals: list[dict[str, Any]]) -> list[ClientResult]:
        self.assertEqual(2, len(proposals))
        snapshot_root = self._state_root()
        release = self.root / f"release-{proposals[0]['proposal_id']}"
        processes: list[tuple[JsonCommitClient, subprocess.Popen[str]]] = []
        ready_paths: list[Path] = []
        for index, (client, proposal) in enumerate(zip(self.clients, proposals)):
            proposal["base_state_root"] = snapshot_root
            path = self.root / f"{proposal['proposal_id']}.json"
            path.write_text(json.dumps(proposal), encoding="utf-8")
            ready = self.root / f"ready-{proposal['proposal_id']}-{index}"
            ready_paths.append(ready)
            processes.append(
                (
                    client,
                    client.start_commit(self.workspace_root, path, ready, release),
                )
            )
        self.assertEqual(snapshot_root, proposals[0]["base_state_root"])
        self.assertEqual(snapshot_root, proposals[1]["base_state_root"])
        deadline = time.monotonic() + 15
        while not all(path.exists() for path in ready_paths):
            if time.monotonic() >= deadline:
                for _, process in processes:
                    process.kill()
                self.fail("independent clients did not reach the release barrier")
            time.sleep(0.005)
        release.touch()

        results = []
        for client, process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual("", stderr, stderr)
            self.assertEqual(1, len(stdout.splitlines()), stdout)
            results.append(
                ClientResult(
                    client.client_id,
                    process.pid,
                    process.returncode,
                    json.loads(stdout),
                )
            )
        self.assertEqual(2, len({result.pid for result in results}))
        self.assertNotIn(os.getpid(), {result.pid for result in results})
        return results

    def _assert_destructive_race(
        self,
        results: list[ClientResult],
        proposals: list[dict[str, Any]],
        ledger_before: int,
    ) -> None:
        self.assertEqual(
            ["COMMITTED", "TRANSACTION_CONFLICT"],
            sorted(result.document["code"] for result in results),
        )
        winner = next(r for r in results if r.document["code"] == "COMMITTED")
        loser = next(r for r in results if r.document["code"] == "TRANSACTION_CONFLICT")
        self.assertEqual(0, winner.exit_code)
        self.assertEqual(4, loser.exit_code)
        self.assertIsNone(loser.document["data"]["decision_receipt"]["ledger_entry_id"])
        self.assertEqual(
            loser.document["data"]["decision_receipt"]["head_before"],
            loser.document["data"]["decision_receipt"]["head_after"],
        )
        self.assertEqual(ledger_before + 1, self._ledger_count())
        self._assert_attempts_auditable(results, proposals)

    def _assert_attempts_auditable(
        self, results: list[ClientResult], proposals: list[dict[str, Any]]
    ) -> None:
        kernel = self.workspace.open_kernel()
        try:
            receipts = {
                item["proposal_id"]: item for item in kernel.decision_receipts()
            }
            accepted = {}
            ledger_rows = kernel.connection.execute(
                "SELECT proposal, events FROM ledger"
            )
            for row in ledger_rows:
                proposal = json.loads(row["proposal"])
                events = json.loads(row["events"])
                accepted[proposal["proposal_id"]] = (proposal, events)
        finally:
            kernel.close()
        result_by_id = {
            item.document["data"]["proposal_id"]: item for item in results
        }
        silent_overwrite_count = 0
        for proposal in proposals:
            proposal_id = proposal["proposal_id"]
            if proposal_id not in receipts:
                silent_overwrite_count += 1
                continue
            self.assertEqual(
                result_by_id[proposal_id].document["code"],
                receipts[proposal_id]["outcome"],
            )
            if proposal_id in accepted:
                stored_proposal, events = accepted[proposal_id]
                self.assertEqual(proposal["proposer"], stored_proposal["proposer"])
                self.assertIn(proposal["proposer"]["actor_id"], json.dumps(events))
            else:
                self.assertIn(
                    "proposer",
                    receipts[proposal_id],
                    "a durable rejected receipt must retain the submitting actor",
                )
                self.assertEqual(
                    proposal["proposer"], receipts[proposal_id]["proposer"]
                )
        self.assertEqual(0, silent_overwrite_count)

    def _assert_replay_root_parity(self) -> None:
        exit_code, report = self._invoke("replay", "--verify")
        self.assertEqual(0, exit_code, report)
        self.assertEqual("LEDGER_VALID", report["code"])
        self.assertTrue(report["data"]["valid"])
        kernel = self.workspace.open_kernel()
        try:
            expected_root = kernel.state_root()
            replayed = kernel.replay(self.root / f"replay-{time.time_ns()}.sqlite3")
            try:
                self.assertEqual(expected_root, replayed.state_root())
                self.assertTrue(replayed.verify_ledger()["valid"])
            finally:
                replayed.close()
        finally:
            kernel.close()

    def _commit_seed(self, fixture: str) -> None:
        proposal = copy.deepcopy(self.objects[fixture])
        exit_code, result = self._commit_sync(proposal)
        self.assertEqual(0, exit_code, result)
        self.assertIn(result["code"], {"COMMITTED", "FACT_CONFLICT"})

    def _commit_sync(self, proposal: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        path = self.root / f"seed-{proposal['proposal_id']}.json"
        path.write_text(json.dumps(proposal), encoding="utf-8")
        return self._invoke("proposal", "commit", str(path), "--json")

    def _invoke(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from shared_mind.cli import main; raise SystemExit(main())",
                "--workspace",
                str(self.workspace_root),
                *arguments,
            ],
            cwd=ROOT,
            env=_python_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual("", completed.stderr, completed.stderr)
        self.assertEqual(1, len(completed.stdout.splitlines()), completed.stdout)
        return completed.returncode, json.loads(completed.stdout)

    def _supersede(self, index: int) -> dict[str, Any]:
        proposal = self._as_actor(
            self.objects["stale_supersede_proposal"], "claim-supersede", index
        )
        operation = proposal["operations"][0]
        claim_id = f"claim_multi_client_replacement_{index:02d}"
        operation["replacement_claim"]["claim_id"] = claim_id
        operation["replacement_claim"]["asserted_by"] = proposal["proposer"]
        operation["initial_evidence"][0]["claim_id"] = claim_id
        operation["initial_evidence"][0]["evidence_link_id"] = (
            f"evidence_multi_client_replacement_{index:02d}"
        )
        operation["initial_evidence"][0]["linked_by"] = proposal["proposer"]
        return proposal

    def _attach(self, index: int, *, unique_key: bool) -> dict[str, Any]:
        proposal = self._as_actor(
            self.objects["assert_postgresql_proposal"], "attach", index
        )
        evidence = copy.deepcopy(proposal["operations"][0]["initial_evidence"][0])
        evidence["evidence_link_id"] = f"evidence_multi_client_attach_{index:02d}"
        evidence["linked_by"] = proposal["proposer"]
        proposal["operations"] = [
            {
                "op_id": f"operation_multi_client_attach_{index:02d}",
                "op": "ATTACH_EVIDENCE",
                "evidence_link": evidence,
            }
        ]
        if not unique_key:
            proposal["idempotency_key"] = "multi-client-reused-key-001"
        return proposal

    def _as_actor(
        self, fixture: dict[str, Any], stem: str, index: int
    ) -> dict[str, Any]:
        proposal = copy.deepcopy(fixture)
        normalized_stem = stem.replace("-", "_")
        proposal["proposal_id"] = (
            f"proposal_multi_client_{normalized_stem}_{index:02d}"
        )
        proposal["idempotency_key"] = f"multi-client-{stem}-{index:02d}"
        actor = {"actor_id": f"agent:client-{index}", "actor_type": "AGENT"}
        proposal["proposer"] = actor
        operation = proposal["operations"][0]
        operation["op_id"] = (
            f"operation_multi_client_{normalized_stem}_{index:02d}"
        )
        if operation["op"] == "ANSWER_QUESTION":
            operation["answer"]["answered_by"] = actor
        elif operation["op"] == "DROP_QUESTION":
            operation["drop"]["dropped_by"] = actor
        return proposal

    def _ledger_count(self) -> int:
        kernel = self.workspace.open_kernel()
        try:
            row = kernel.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()
            return int(row[0])
        finally:
            kernel.close()

    def _state_root(self) -> str:
        kernel = self.workspace.open_kernel()
        try:
            return kernel.state_root()
        finally:
            kernel.close()


def _python_environment() -> dict[str, str]:
    environment = os.environ.copy()
    current = environment.get("PYTHONPATH")
    source = str(ROOT / "src")
    environment["PYTHONPATH"] = source if not current else source + os.pathsep + current
    return environment


if __name__ == "__main__":
    unittest.main()
