from __future__ import annotations

import base64
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from shared_mind import Kernel


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "durability"
COMMIT_WORKER = FIXTURE_ROOT / "commit_worker.py"
SNAPSHOT_WORKER = FIXTURE_ROOT / "snapshot_worker.py"


@unittest.skipUnless(os.name == "posix", "process durability uses POSIX SIGKILL")
class ProcessDurabilityTest(unittest.TestCase):
    """NFR-002/003 process-kill and WAL recovery acceptance tests."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "shared-mind.sqlite3"
        self.registry_path = (
            ROOT / "contracts" / "atlas-predicate-registry.v1.json"
        )
        self.registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        fixtures = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.objects = {
            item["name"]: item["object"] for item in fixtures["typed_objects"]
        }
        self.proposal = self._source_proposal()
        self.proposal_path = self.root / "proposal.json"
        self.proposal_path.write_text(
            json.dumps(self.proposal, ensure_ascii=False), encoding="utf-8"
        )
        kernel = Kernel(self.database, self.registry)
        self.empty_state_root = kernel.state_root()
        self.assertEqual(
            "wal", kernel.connection.execute("PRAGMA journal_mode").fetchone()[0]
        )
        kernel.close()
        self.processes: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
        self.temp.cleanup()

    def test_wal_commits_use_full_synchronous_durability(self) -> None:
        kernel = Kernel(self.database, self.registry)
        try:
            self.assertEqual(
                "wal", kernel.connection.execute("PRAGMA journal_mode").fetchone()[0]
            )
            self.assertEqual(
                2,
                kernel.connection.execute("PRAGMA synchronous").fetchone()[0],
                "accepted mutations require SQLite FULL synchronous durability",
            )
        finally:
            kernel.close()

    def test_kill_before_sqlite_commit_leaves_no_partial_canonical_mutation(self) -> None:
        process, ready, _ = self._start_worker("after_operation")
        self._wait_for_barrier(process, ready)

        self._kill(process)

        snapshot = self._snapshot()
        self.assertEqual(
            {
                "accepted_receipts": 0,
                "ledger": 0,
                "receipts": 0,
                "sources": 0,
                "state_root": self.empty_state_root,
            },
            self._without_pid(snapshot),
        )
        kernel = Kernel(self.database, self.registry)
        try:
            self.assertTrue(kernel.verify_ledger()["valid"])
        finally:
            kernel.close()

    def test_kill_after_durable_commit_preserves_one_entry_and_replay_parity(self) -> None:
        process, ready, _ = self._start_worker("after_commit")
        committed = self._wait_for_barrier(process, ready)
        self.assertEqual("COMMITTED", committed["outcome"])
        self.assertEqual(1, committed["ledger_seq"])

        self._kill(process)

        snapshot = self._snapshot()
        self.assertEqual(
            (1, 1, 1),
            (
                snapshot["ledger"],
                snapshot["sources"],
                snapshot["accepted_receipts"],
            ),
        )
        kernel = Kernel(self.database, self.registry)
        try:
            self.assertTrue(kernel.verify_ledger()["valid"])
            replayed = kernel.replay(self.root / "replayed.sqlite3")
            try:
                self.assertEqual(kernel.state_root(), replayed.state_root())
                self.assertEqual(kernel.ledger_entries(), replayed.ledger_entries())
            finally:
                replayed.close()
        finally:
            kernel.close()

    def test_retry_after_uncertain_committed_outcome_is_exactly_once(self) -> None:
        process, ready, _ = self._start_worker("after_commit")
        committed = self._wait_for_barrier(process, ready)
        self._kill(process)

        kernel = Kernel(self.database, self.registry)
        try:
            retry = kernel.commit(copy.deepcopy(self.proposal))
            self.assertEqual("COMMITTED", retry.outcome)
            self.assertEqual(committed["ledger_seq"], retry.ledger_seq)
            self.assertEqual(committed["state_root"], retry.state_root)
            self.assertEqual(committed["document"], retry.document)
            self.assertEqual(
                1,
                kernel.connection.execute(
                    "SELECT COUNT(*) FROM ledger WHERE proposal = ?",
                    (
                        json.dumps(
                            self.proposal,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ).fetchone()[0],
            )
            receipts = kernel.connection.execute(
                "SELECT outcome, ledger_seq FROM receipts WHERE idempotency_key = ?",
                (self.proposal["idempotency_key"],),
            ).fetchall()
            self.assertEqual(
                [("COMMITTED", 1)],
                [(row["outcome"], row["ledger_seq"]) for row in receipts],
            )
        finally:
            kernel.close()

    def test_independent_reader_sees_only_pre_or_post_commit_state(self) -> None:
        process, ready, release = self._start_worker("after_operation")
        self._wait_for_barrier(process, ready)
        before = self._snapshot()
        self.assertNotEqual(os.getpid(), before["pid"])
        self.assertEqual(
            (0, 0, 0),
            (before["ledger"], before["sources"], before["receipts"]),
        )

        release.touch()
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual("", stderr, stderr)
        self.assertEqual(0, process.returncode, stdout)
        committed = json.loads(stdout)
        self.assertEqual("COMMITTED", committed["outcome"])
        after = self._snapshot()
        self.assertNotEqual(os.getpid(), after["pid"])
        self.assertEqual(
            (1, 1, 1),
            (after["ledger"], after["sources"], after["receipts"]),
        )
        observed = {
            (item["ledger"], item["sources"], item["receipts"])
            for item in (before, after)
        }
        self.assertEqual({(0, 0, 0), (1, 1, 1)}, observed)

    def test_incomplete_wal_tail_recovers_committed_entry_without_overwrite(self) -> None:
        self._leave_committed_wal_from_killed_process()
        wal = self.database.with_name(self.database.name + "-wal")
        self.assertTrue(wal.is_file(), "worker must leave an uncheckpointed WAL")
        with wal.open("ab") as handle:
            handle.write(b"INCOMPLETE-WAL-FRAME" * 3)

        kernel = Kernel(self.database, self.registry)
        try:
            self.assertEqual(1, self._count(kernel, "ledger"))
            self.assertEqual(1, self._count(kernel, "sources"))
            self.assertEqual(1, self._count(kernel, "receipts"))
            self.assertTrue(kernel.verify_ledger()["valid"])
        finally:
            kernel.close()

    def test_corrupt_committed_wal_fails_closed_instead_of_silent_revert(self) -> None:
        self._leave_committed_wal_from_killed_process()
        wal = self.database.with_name(self.database.name + "-wal")
        shm = self.database.with_name(self.database.name + "-shm")
        content = bytearray(wal.read_bytes())
        self.assertGreater(len(content), 64, "fixture must contain at least one WAL frame")
        content[-1] ^= 0xFF
        wal.write_bytes(content)
        shm.unlink(missing_ok=True)

        try:
            kernel = Kernel(self.database, self.registry)
        except Exception:
            return
        try:
            ledger_count = self._count(kernel, "ledger")
            source_count = self._count(kernel, "sources")
            accepted_count = kernel.connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE ledger_seq IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(
                (1, 1, 1),
                (ledger_count, source_count, accepted_count),
                "a corrupt committed WAL must not be silently treated as an empty valid history",
            )
            self.assertTrue(kernel.verify_ledger()["valid"])
        finally:
            kernel.close()

    def test_orphan_temp_artifact_cannot_replace_the_canonical_database(self) -> None:
        process, _, release = self._start_worker("none")
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual("", stderr, stderr)
        self.assertEqual(0, process.returncode, stdout)
        release.touch()
        artifact = self.database.with_name(self.database.name + ".tmp-interrupted")
        shutil.copyfile(self.database, artifact)
        artifact.write_bytes(b"not-a-sqlite-database")

        kernel = Kernel(self.database, self.registry)
        try:
            self.assertEqual(1, self._count(kernel, "ledger"))
            self.assertEqual(1, self._count(kernel, "sources"))
            self.assertTrue(kernel.verify_ledger()["valid"])
        finally:
            kernel.close()

    def _leave_committed_wal_from_killed_process(self) -> dict[str, Any]:
        process, ready, _ = self._start_worker("after_commit")
        committed = self._wait_for_barrier(process, ready)
        self.assertEqual("COMMITTED", committed["outcome"])
        self._kill(process)
        return committed

    def _start_worker(
        self, stage: str
    ) -> tuple[subprocess.Popen[str], Path, Path]:
        index = len(self.processes)
        ready = self.root / f"ready-{stage}-{index}"
        release = self.root / f"release-{stage}-{index}"
        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMIT_WORKER),
                "--database",
                str(self.database),
                "--registry",
                str(self.registry_path),
                "--proposal",
                str(self.proposal_path),
                "--barrier-stage",
                stage,
                "--ready",
                str(ready),
                "--release",
                str(release),
            ],
            cwd=ROOT,
            env=self._python_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        return process, ready, release

    def _wait_for_barrier(
        self, process: subprocess.Popen[str], ready: Path
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 15
        while not ready.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                self.fail(
                    f"worker exited before barrier ({process.returncode}): {stdout}\n{stderr}"
                )
            if time.monotonic() >= deadline:
                self._kill(process)
                self.fail(f"worker did not reach barrier: {ready.name}")
            time.sleep(0.005)
        return json.loads(ready.read_text(encoding="utf-8"))

    def _kill(self, process: subprocess.Popen[str]) -> None:
        process.send_signal(signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual("", stdout, stdout)
        self.assertEqual("", stderr, stderr)
        self.assertEqual(-signal.SIGKILL, process.returncode)

    def _snapshot(self) -> dict[str, Any]:
        completed = subprocess.run(
            [
                sys.executable,
                str(SNAPSHOT_WORKER),
                "--database",
                str(self.database),
                "--registry",
                str(self.registry_path),
            ],
            cwd=ROOT,
            env=self._python_environment(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr, completed.stderr)
        return json.loads(completed.stdout)

    def _source_proposal(self) -> dict[str, Any]:
        source = copy.deepcopy(self.objects["source_revision_postgresql"])
        content = (ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes()
        source["blob_ref"] = "data:text/markdown;base64," + base64.b64encode(
            content
        ).decode("ascii")
        versions = copy.deepcopy(
            self.objects["assert_postgresql_proposal"]["versions"]
        )
        return {
            "object_type": "PROPOSAL",
            "proposal_id": "proposal_durability_source_001",
            "idempotency_key": "durability-source-commit-001",
            "proposer": copy.deepcopy(source["registered_by"]),
            "proposed_at": source["captured_at"],
            "base_state_root": None,
            "versions": versions,
            "reads": [],
            "guards": [],
            "operations": [
                {
                    "op_id": "operation_durability_source_001",
                    "op": "REGISTER_SOURCE_REVISION",
                    "source_revision": source,
                }
            ],
        }

    @staticmethod
    def _count(kernel: Kernel, table: str) -> int:
        return int(
            kernel.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )

    @staticmethod
    def _without_pid(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in snapshot.items() if key != "pid"}

    @staticmethod
    def _python_environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        return environment


if __name__ == "__main__":
    unittest.main()
