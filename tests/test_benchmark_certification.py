from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "benchmarks" / "context-benchmark-certification.schema.v1.json"
RESULTS = {
    "history-heavy": ROOT
    / "benchmarks"
    / "results"
    / "dev-089-schema13-history-heavy-2026-08-15.json",
    "hot-active": ROOT
    / "benchmarks"
    / "results"
    / "dev-089-schema13-hot-active-2026-08-15.json",
}


class BenchmarkCertificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="shared-mind-certification-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_certification_schema_is_strict_draft_2020_12(self) -> None:
        self.assertTrue(SCHEMA_PATH.is_file())
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            "context-benchmark-certification@1",
            schema["properties"]["certification_version"]["const"],
        )

    def test_small_current_schema_fixture_certifies_verify_replay_and_context(
        self,
    ) -> None:
        from benchmarks.certify_100k import certify_context_benchmark

        result = certify_context_benchmark(
            self.root / "source.sqlite3",
            self.root / "replay.sqlite3",
            ledger_entries=16,
            profile="history-heavy",
            seed=89,
            implementation_id="test-dev089-history",
            warmups=1,
            samples=3,
            budget_bytes=4_096,
        )

        self._validate(result)
        self.assertTrue(result["certified"])
        self.assertEqual(Kernel.SUPPORTED_VERSIONS["schema"], result["schema_version"])
        self.assertEqual(16, result["fixture"]["ledger_entries"])
        self.assertEqual(16, result["verification"]["checked_entries"])
        self.assertEqual([], result["verification"]["errors"])
        self.assertTrue(result["replay"]["parity"])
        self.assertEqual(result["replay"]["source"], result["replay"]["target"])
        self.assertEqual(16, result["replay"]["source"]["receipt_count"])
        self.assertEqual(3, result["context"]["latency"]["sample_count"])
        self.assertNotIn(str(self.root), canonical_json(result))

    def test_hot_active_profile_retains_bounded_history_and_certifies(self) -> None:
        from benchmarks.certify_100k import certify_context_benchmark

        result = certify_context_benchmark(
            self.root / "hot.sqlite3",
            self.root / "hot-replay.sqlite3",
            ledger_entries=24,
            profile="hot-active",
            seed=90,
            implementation_id="test-dev089-hot",
            warmups=1,
            samples=2,
            budget_bytes=4_096,
        )

        self._validate(result)
        self.assertEqual("DOING", result["fixture"]["hot_object"]["status"])
        self.assertTrue(result["context"]["target_met"])
        self.assertLessEqual(result["context"]["context_rendered_bytes"], 4_096)

    def test_certification_hash_and_no_clobber_writer_are_deterministic(self) -> None:
        from benchmarks.certify_100k import write_certification_result

        result = self._minimal_result()
        output = self.root / "result.json"

        write_certification_result(result, output)

        self.assertEqual(
            canonical_json(result) + "\n", output.read_text(encoding="utf-8")
        )
        with self.assertRaises(FileExistsError):
            write_certification_result(result, output)

    def test_invalid_verification_fails_closed_before_replay(self) -> None:
        from benchmarks.certify_100k import CertificationError, certify_context_benchmark

        invalid = {
            "valid": False,
            "checked_entries": 8,
            "head_hash": "sha256:" + "0" * 64,
            "state_root": "sha256:" + "0" * 64,
            "errors": ["FORGED"],
        }
        replay = self.root / "invalid-replay.sqlite3"

        with mock.patch.object(Kernel, "verify_ledger", return_value=invalid):
            with self.assertRaises(CertificationError) as caught:
                certify_context_benchmark(
                    self.root / "invalid.sqlite3",
                    replay,
                    ledger_entries=8,
                    profile="history-heavy",
                    seed=91,
                    implementation_id="test-dev089-invalid",
                    warmups=1,
                    samples=1,
                    budget_bytes=4_096,
                )

        self.assertEqual("LEDGER_VERIFICATION_FAILED", caught.exception.code)
        self.assertFalse(replay.exists())

    def test_historical_schema_manifest_is_rejected(self) -> None:
        from benchmarks import certify_100k

        original = certify_100k.build_benchmark_fixture

        def historical(*args, **kwargs):
            manifest = original(*args, **kwargs)
            return {**manifest, "schema_version": "1.2.0"}

        with mock.patch.object(
            certify_100k, "build_benchmark_fixture", side_effect=historical
        ):
            with self.assertRaises(certify_100k.CertificationError) as caught:
                certify_100k.certify_context_benchmark(
                    self.root / "historical.sqlite3",
                    self.root / "historical-replay.sqlite3",
                    ledger_entries=4,
                    profile="history-heavy",
                    seed=92,
                    implementation_id="test-dev089-historical",
                    warmups=1,
                    samples=1,
                    budget_bytes=4_096,
                )

        self.assertEqual("SCHEMA_VERSION_MISMATCH", caught.exception.code)

    def test_checked_in_fresh_schema_1_3_certifications_are_complete(self) -> None:
        for profile, result_path in RESULTS.items():
            with self.subTest(profile=profile):
                self.assertTrue(result_path.is_file())
                result = json.loads(result_path.read_text(encoding="utf-8"))
                self._validate(result)
                self.assertTrue(result["certified"])
                self.assertEqual(profile, result["profile"])
                self.assertEqual(100_000, result["requested_ledger_entries"])
                self.assertEqual("1.3.0", result["schema_version"])
                self.assertEqual("1.3.0", result["fixture"]["schema_version"])
                self.assertEqual(100_000, result["fixture"]["ledger_entries"])
                self.assertEqual(100_000, result["verification"]["checked_entries"])
                self.assertEqual([], result["verification"]["errors"])
                self.assertTrue(result["replay"]["parity"])
                self.assertEqual(
                    result["replay"]["source"], result["replay"]["target"]
                )
                self.assertEqual(
                    ["1.3.0"],
                    result["replay"]["source"]["receipt_schema_versions"],
                )
                self.assertEqual(50, result["context"]["latency"]["sample_count"])
                self.assertTrue(result["context"]["target_met"])
                self.assertLessEqual(
                    result["context"]["context_rendered_bytes"],
                    result["context"]["budget_bytes"],
                )

    def _validate(self, result: dict) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(result)
        payload = {key: value for key, value in result.items() if key != "certification_hash"}
        self.assertEqual(sha256_json(payload), result["certification_hash"])

    def _minimal_result(self) -> dict:
        from benchmarks.certify_100k import certify_context_benchmark

        result = certify_context_benchmark(
            self.root / "writer-source.sqlite3",
            self.root / "writer-replay.sqlite3",
            ledger_entries=2,
            profile="history-heavy",
            seed=93,
            implementation_id="test-dev089-writer",
            warmups=1,
            samples=1,
            budget_bytes=4_096,
        )
        self._validate(result)
        return result


if __name__ == "__main__":
    unittest.main()
