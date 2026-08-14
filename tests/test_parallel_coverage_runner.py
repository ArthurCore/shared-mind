from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "run_parallel_coverage.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class ParallelCoverageRunnerTest(unittest.TestCase):
    def test_process_heavy_sqlite_suites_use_an_exclusive_serial_lane(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        for filename in (
            "test_concurrency.py",
            "test_multi_client_acceptance.py",
            "test_process_durability.py",
        ):
            with self.subTest(filename=filename):
                self.assertIn(filename, source)

        self.assertIn("_EXCLUSIVE_TEST_FILES", source)
        self.assertIn("exclusive_files =", source)
        self.assertIn("parallel_files =", source)
        self.assertIn("for test_path in exclusive_files", source)
        self.assertGreater(
            source.index("for test_path in exclusive_files"),
            source.index("ThreadPoolExecutor"),
            "exclusive SQLite suites must run after the broad parallel pool",
        )

    def test_ci_retains_per_file_logs_for_remote_failure_diagnosis(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            '.ci/test-results/per-file-${{ matrix.python-version }}/',
            workflow,
        )
        self.assertIn("actions/upload-artifact@", workflow)


if __name__ == "__main__":
    unittest.main()
