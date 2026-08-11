from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from shared_mind.cli import (
    EXIT_OK,
    EXIT_VALIDATION_ERROR,
    main,
)


ROOT = Path(__file__).resolve().parents[1]


class SharedMindCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temp.name) / "workspace"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self, *arguments: str) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(list(arguments), stdout=stdout, stderr=stderr)
        raw = stdout.getvalue()
        self.assertTrue(raw.endswith("\n"), raw)
        self.assertEqual(1, len(raw.splitlines()), raw)
        return exit_code, json.loads(raw), stderr.getvalue()

    def initialize(self) -> dict[str, object]:
        exit_code, result, stderr = self.invoke("init", str(self.workspace_root))
        self.assertEqual(EXIT_OK, exit_code)
        self.assertEqual("", stderr)
        self.assertTrue(result["ok"])
        self.assertEqual("WORKSPACE_INITIALIZED", result["code"])
        return result

    def test_fr_001_init_creates_a_reproducible_local_workspace(self) -> None:
        first = self.initialize()
        config_path = self.workspace_root / ".shared-mind" / "workspace.json"
        database_path = self.workspace_root / ".shared-mind" / "shared-mind.sqlite3"
        registry_path = self.workspace_root / ".shared-mind" / "predicate-registry.json"
        original_config = config_path.read_bytes()

        second = self.initialize()

        self.assertEqual(original_config, config_path.read_bytes())
        self.assertTrue(database_path.is_file())
        self.assertTrue(registry_path.is_file())
        self.assertTrue((self.workspace_root / "sources").is_dir())
        self.assertTrue((self.workspace_root / "projections").is_dir())
        self.assertTrue((self.workspace_root / ".shared-mind" / "blobs").is_dir())
        self.assertEqual(first["data"], second["data"])
        config = json.loads(original_config)
        self.assertFalse(Path(config["database"]).is_absolute())
        self.assertFalse(Path(config["source_root"]).is_absolute())

    def test_nfr_010_source_add_rejects_path_outside_source_root(self) -> None:
        self.initialize()
        outside = Path(self.temp.name) / "private.txt"
        outside.write_text("must not be read", encoding="utf-8")

        exit_code, result, _ = self.invoke(
            "--workspace", str(self.workspace_root), "source", "add", str(outside)
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertFalse(result["ok"])
        self.assertEqual("PATH_OUTSIDE_SOURCE_ROOT", result["code"])

    def test_nfr_010_source_add_rejects_symlink_escape(self) -> None:
        self.initialize()
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        link = self.workspace_root / "sources" / "escape.md"
        try:
            os.symlink(outside, link)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are not available")

        exit_code, result, _ = self.invoke(
            "--workspace", str(self.workspace_root), "source", "add", str(link)
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("PATH_OUTSIDE_SOURCE_ROOT", result["code"])

    def test_fr_002_source_add_is_idempotent_and_preserves_content(self) -> None:
        self.initialize()
        source = self.workspace_root / "sources" / "notes.md"
        source.write_text("# Notes\n\nShared memory.\n", encoding="utf-8")

        first_code, first, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "source",
            "add",
            str(source),
            "--source-id",
            "document:notes",
        )
        second_code, second, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "source",
            "add",
            str(source),
            "--source-id",
            "document:notes",
        )

        self.assertEqual(EXIT_OK, first_code)
        self.assertEqual(EXIT_OK, second_code)
        self.assertEqual("SOURCE_REGISTERED", first["code"])
        self.assertEqual(first["data"], second["data"])
        self.assertIsInstance(first["data"]["ledger_sequence"], int)
        self.assertEqual(1, self._database_count("ledger"))
        blob = self.workspace_root / first["data"]["blob_path"]
        self.assertEqual(source.read_bytes(), blob.read_bytes())

    def test_fr_002_source_add_rejects_non_utf8_and_unknown_media_type(self) -> None:
        self.initialize()
        binary = self.workspace_root / "sources" / "bytes.txt"
        binary.write_bytes(b"\xff\xfe")
        unsupported = self.workspace_root / "sources" / "image.png"
        unsupported.write_bytes(b"png")

        binary_code, binary_result, _ = self.invoke(
            "--workspace", str(self.workspace_root), "source", "add", str(binary)
        )
        media_code, media_result, _ = self.invoke(
            "--workspace", str(self.workspace_root), "source", "add", str(unsupported)
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, binary_code)
        self.assertEqual("SOURCE_NOT_UTF8", binary_result["code"])
        self.assertEqual(EXIT_VALIDATION_ERROR, media_code)
        self.assertEqual("UNSUPPORTED_SOURCE_MEDIA_TYPE", media_result["code"])

    def test_fr_050_proposal_validate_returns_detailed_json_without_mutation(self) -> None:
        self.initialize()
        proposal = self._fixture("assert_postgresql_proposal")
        proposal_path = self.workspace_root / "proposal.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

        valid_code, valid, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "validate",
            str(proposal_path),
        )
        del proposal["idempotency_key"]
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        invalid_code, invalid, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "validate",
            str(proposal_path),
        )

        self.assertEqual(EXIT_OK, valid_code)
        self.assertEqual("PROPOSAL_VALID", valid["code"])
        self.assertEqual(EXIT_VALIDATION_ERROR, invalid_code)
        self.assertEqual("PROPOSAL_INVALID", invalid["code"])
        self.assertEqual("SCHEMA_VALIDATION_FAILED", invalid["errors"][0]["code"])
        self.assertIn("idempotency_key", invalid["errors"][0]["message"])
        self.assertEqual(0, self._database_count("ledger"))

    def test_fr_050_invalid_json_is_a_stable_machine_readable_error(self) -> None:
        self.initialize()
        proposal_path = self.workspace_root / "bad.json"
        proposal_path.write_text("{", encoding="utf-8")

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "commit",
            str(proposal_path),
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("MALFORMED_JSON", result["code"])
        self.assertIn("message", result)

    def test_fr_050_proposal_commit_returns_receipt_json(self) -> None:
        source_result = self._register_fixture_source()
        proposal = self._fixture("assert_postgresql_proposal")
        proposal["operations"][0]["initial_evidence"][0]["source_revision_id"] = (
            source_result["data"]["revision_id"]
        )
        proposal_path = self.workspace_root / "proposal.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "commit",
            str(proposal_path),
        )

        self.assertEqual(EXIT_OK, exit_code)
        self.assertEqual("COMMITTED", result["code"])
        self.assertEqual(proposal["proposal_id"], result["data"]["proposal_id"])
        self.assertIsInstance(result["data"]["ledger_sequence"], int)
        self.assertTrue(result["data"]["state_root"].startswith("sha256:"))

    def test_fr_051_conflict_list_is_json_and_read_only(self) -> None:
        self.initialize()

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "conflict",
            "list",
            "--status",
            "OPEN",
        )

        self.assertEqual(EXIT_OK, exit_code)
        self.assertEqual("CONFLICTS_LISTED", result["code"])
        self.assertEqual([], result["data"]["conflicts"])
        self.assertEqual(0, self._database_count("ledger"))

    def test_fr_051_conflict_resolve_rejects_a_proposal_for_another_conflict(self) -> None:
        self.initialize()
        proposal = self._fixture("assert_postgresql_proposal")
        proposal_path = self.workspace_root / "proposal.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "conflict",
            "resolve",
            "conflict_expected_12345678",
            "--proposal",
            str(proposal_path),
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("CONFLICT_PROPOSAL_MISMATCH", result["code"])
        self.assertEqual(0, self._database_count("ledger"))

    def test_fr_041_replay_verify_reports_chain_status(self) -> None:
        self.initialize()

        exit_code, result, _ = self.invoke(
            "--workspace", str(self.workspace_root), "replay", "--verify"
        )

        self.assertEqual(EXIT_OK, exit_code)
        self.assertEqual("LEDGER_VALID", result["code"])
        self.assertTrue(result["data"]["valid"])

    def test_fr_042_and_fr_044_projection_commands_are_json_wrapped(self) -> None:
        self.initialize()

        project_code, project, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "project",
            "--format",
            "markdown",
        )
        context_code, context, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "context",
            "--budget-tokens",
            "256",
        )
        json_code, json_project, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "project",
            "--format",
            "json",
        )

        self.assertEqual(EXIT_OK, project_code)
        self.assertEqual("PROJECTED", project["code"])
        self.assertIsInstance(project["data"]["content"], str)
        self.assertEqual(EXIT_OK, context_code)
        self.assertEqual("CONTEXT_READY", context["code"])
        self.assertIn("context", context["data"])
        self.assertEqual(EXIT_OK, json_code)
        self.assertIsInstance(json.loads(json_project["data"]["content"]), dict)

    def test_nfr_009_context_budget_error_is_machine_readable(self) -> None:
        self.initialize()

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "context",
            "--budget-bytes",
            "1",
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("CONTEXT_BUDGET_TOO_SMALL", result["code"])
        self.assertIn("required_bytes", result["data"])

    def _register_fixture_source(self) -> dict[str, object]:
        self.initialize()
        source = self.workspace_root / "sources" / "atlas-runbook.md"
        source.write_bytes((ROOT / "contracts" / "atlas-runbook.fixture.md").read_bytes())
        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "source",
            "add",
            str(source),
            "--source-id",
            "document:atlas-runbook",
        )
        self.assertEqual(EXIT_OK, exit_code)
        return result

    def _fixture(self, name: str) -> dict[str, object]:
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text()
        )
        objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        return copy.deepcopy(objects[name])

    def _database_count(self, table: str) -> int:
        import sqlite3

        database = self.workspace_root / ".shared-mind" / "shared-mind.sqlite3"
        with sqlite3.connect(database) as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
