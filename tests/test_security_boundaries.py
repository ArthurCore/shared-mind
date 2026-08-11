from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared_mind.cli import EXIT_OK, EXIT_VALIDATION_ERROR, main
from shared_mind.service import WorkspaceService
from shared_mind.workspace import Workspace, WorkspaceError


ROOT = Path(__file__).resolve().parents[1]
MAX_JSON_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64


class SecurityBoundaryTest(unittest.TestCase):
    """NFR-010 boundaries shared by the CLI and future local adapters."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp.name)
        self.workspace_root = self.temp_root / "workspace"
        self.workspace = Workspace.initialize(self.workspace_root)
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }

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

    def test_nfr_010_proposal_json_inside_workspace_is_allowed(self) -> None:
        proposal_path = self.workspace_root / "proposals" / "valid.json"
        proposal_path.parent.mkdir()
        self._write_json(proposal_path, self.objects["assert_postgresql_proposal"])

        exit_code, result, stderr = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "validate",
            str(proposal_path),
        )

        self.assertEqual(EXIT_OK, exit_code)
        self.assertEqual("PROPOSAL_VALID", result["code"])
        self.assertEqual("", stderr)
        self.assertEqual((0, 0), self._canonical_counts())

    def test_nfr_010_proposal_json_outside_workspace_is_rejected(self) -> None:
        outside = self.temp_root / "outside-proposal.json"
        self._write_json(outside, self.objects["assert_postgresql_proposal"])

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "validate",
            str(outside),
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("PATH_OUTSIDE_WORKSPACE", result["code"])
        self.assertEqual((0, 0), self._canonical_counts())

    def test_nfr_010_proposal_path_traversal_is_rejected(self) -> None:
        outside = self.temp_root / "traversed-proposal.json"
        self._write_json(outside, self.objects["assert_postgresql_proposal"])
        traversal = self.workspace_root / "proposals" / ".." / ".." / outside.name

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "commit",
            str(traversal),
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("PATH_OUTSIDE_WORKSPACE", result["code"])
        self.assertEqual((0, 0), self._canonical_counts())

    def test_nfr_010_proposal_symlink_escape_is_rejected(self) -> None:
        outside = self.temp_root / "symlink-target.json"
        self._write_json(outside, self.objects["assert_postgresql_proposal"])
        link = self.workspace_root / "proposal-link.json"
        try:
            os.symlink(outside, link)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are not available")

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "validate",
            str(link),
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("PATH_OUTSIDE_WORKSPACE", result["code"])
        self.assertEqual((0, 0), self._canonical_counts())

    def test_nfr_010_source_traversal_and_symlink_escape_do_not_mutate(self) -> None:
        outside = self.temp_root / "private.md"
        outside.write_text("must not be ingested", encoding="utf-8")
        traversal = self.workspace_root / "sources" / ".." / ".." / outside.name
        link = self.workspace.source_root / "private-link.md"
        try:
            os.symlink(outside, link)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are not available")

        for candidate in (traversal, link):
            with self.subTest(candidate=candidate):
                with self.assertRaises(WorkspaceError) as caught:
                    self.workspace.resolve_source_input(candidate)
                self.assertEqual("PATH_OUTSIDE_SOURCE_ROOT", caught.exception.code)
                self.assertEqual((0, 0), self._canonical_counts())

    def test_nfr_010_configured_paths_cannot_escape_workspace(self) -> None:
        config_path = self.workspace.config_path
        original = json.loads(config_path.read_text(encoding="utf-8"))
        outside_directory = self.temp_root / "outside-directory"
        outside_directory.mkdir()
        link = self.workspace_root / "outside-link"
        try:
            os.symlink(outside_directory, link)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are not available")
        cases = (
            ("database", str(self.temp_root / "escaped.sqlite3")),
            ("source_root", "../outside-directory"),
            ("source_root", "outside-link"),
        )

        for field, value in cases:
            with self.subTest(field=field, value=value):
                modified = dict(original)
                modified[field] = value
                self._write_json(config_path, modified)
                with self.assertRaises(WorkspaceError) as caught:
                    Workspace.open(self.workspace_root)
                self.assertEqual("WORKSPACE_CONFIG_INVALID", caught.exception.code)
        self._write_json(config_path, original)

    def test_nfr_010_malformed_json_fails_before_receipt_or_ledger_write(self) -> None:
        proposal_path = self.workspace_root / "malformed.json"
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
        self.assertEqual((0, 0), self._canonical_counts())

    def test_nfr_010_oversized_json_fails_before_decode_or_mutation(self) -> None:
        proposal_path = self.workspace_root / "oversized.json"
        proposal_path.write_bytes(b'"' + (b"x" * MAX_JSON_BYTES) + b'"')

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "commit",
            str(proposal_path),
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("JSON_TOO_LARGE", result["code"])
        self.assertEqual((0, 0), self._canonical_counts())

    def test_nfr_010_excessive_json_depth_fails_before_mutation(self) -> None:
        proposal_path = self.workspace_root / "too-deep.json"
        nested = ("[" * (MAX_JSON_DEPTH + 1)) + "0" + ("]" * (MAX_JSON_DEPTH + 1))
        proposal_path.write_text(nested, encoding="utf-8")

        exit_code, result, _ = self.invoke(
            "--workspace",
            str(self.workspace_root),
            "proposal",
            "commit",
            str(proposal_path),
        )

        self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
        self.assertEqual("JSON_TOO_DEEP", result["code"])
        self.assertEqual((0, 0), self._canonical_counts())

    def test_nfr_010_decoded_proposal_has_a_transport_independent_size_limit(
        self,
    ) -> None:
        proposal = {
            "object_type": "PROPOSAL",
            "proposal_id": "proposal_oversized_boundary_001",
            "idempotency_key": "oversized-boundary-001",
            "padding": "x" * MAX_JSON_BYTES,
        }

        result = WorkspaceService(self.workspace).commit_proposal(proposal)

        self.assertFalse(result.ok)
        self.assertEqual("VALIDATION_ERROR", result.code)
        self.assertIsInstance(result.data, dict)
        assert isinstance(result.data, dict)
        self.assertEqual(["PROPOSAL_TOO_LARGE"], result.data["reason_codes"])
        self.assertEqual((0, 1), self._canonical_counts())

    def test_nfr_010_decoded_proposal_has_a_transport_independent_depth_limit(
        self,
    ) -> None:
        nested: object = 0
        for _ in range(MAX_JSON_DEPTH + 1):
            nested = {"nested": nested}
        proposal = {
            "object_type": "PROPOSAL",
            "proposal_id": "proposal_too_deep_boundary_001",
            "idempotency_key": "aaaaaaaaaaaaaaaaaaaa",
            "padding": nested,
        }

        result = WorkspaceService(self.workspace).commit_proposal(proposal)

        self.assertFalse(result.ok)
        self.assertEqual("VALIDATION_ERROR", result.code)
        self.assertIsInstance(result.data, dict)
        assert isinstance(result.data, dict)
        self.assertEqual(["PROPOSAL_TOO_DEEP"], result.data["reason_codes"])
        self.assertEqual((0, 1), self._canonical_counts())

    def test_nfr_010_sql_like_source_values_remain_data(self) -> None:
        injection = "'); DROP TABLE ledger; DELETE FROM receipts; --"
        source_path = self.workspace.source_root / f"notes {injection}.md"
        source_path.write_text(injection, encoding="utf-8")

        source, receipt = self.workspace.add_source(
            source_path, source_id="document:sql-payload"
        )

        self.assertEqual("COMMITTED", receipt.outcome)
        kernel = self.workspace.open_kernel()
        try:
            table_names = {
                str(row[0])
                for row in kernel.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            row = kernel.connection.execute(
                "SELECT document, content FROM sources WHERE revision_id = ?",
                (source["revision_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(source_path.name, json.loads(row["document"])["title"])
            self.assertEqual(injection.encode("utf-8"), bytes(row["content"]))
            self.assertIn("ledger", table_names)
            self.assertIn("receipts", table_names)
        finally:
            kernel.close()

    def test_nfr_010_public_sql_surface_denies_file_and_schema_escape(self) -> None:
        kernel = self.workspace.open_kernel()
        attached = self.temp_root / "attached.sqlite3"
        vacuumed = self.temp_root / "vacuum-copy.sqlite3"
        attempts = (
            ("ATTACH DATABASE ? AS escaped", (str(attached),)),
            ("VACUUM INTO ?", (str(vacuumed),)),
            ("PRAGMA writable_schema = ON", ()),
            ("CREATE VIRTUAL TABLE temp.escape_search USING fts5(content)", ()),
        )
        try:
            for statement, parameters in attempts:
                with self.subTest(statement=statement):
                    with self.assertRaises((sqlite3.DatabaseError, PermissionError)):
                        kernel.connection.execute(statement, parameters)
            with self.assertRaises(AttributeError):
                kernel.connection.executescript("DROP TABLE ledger;")
            writable = kernel.connection.execute("PRAGMA writable_schema").fetchone()
            ledger = kernel.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ledger'"
            ).fetchone()
            self.assertEqual(0, writable[0])
            self.assertIsNotNone(ledger)
            self.assertFalse(attached.exists())
            self.assertFalse(vacuumed.exists())
        finally:
            kernel.close()

    def test_nfr_010_secret_like_environment_is_not_emitted_by_context(self) -> None:
        secret_name = "SHARED_MIND_TEST_SENSITIVE_VALUE"
        secret_value = "private-sentinel-must-not-be-emitted-7f91"

        with mock.patch.dict(os.environ, {secret_name: secret_value}):
            exit_code, result, stderr = self.invoke(
                "--workspace", str(self.workspace_root), "context"
            )

        rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
        self.assertEqual(EXIT_OK, exit_code)
        self.assertEqual("CONTEXT_READY", result["code"])
        self.assertEqual("", stderr)
        self.assertNotIn(secret_name, rendered)
        self.assertNotIn(secret_value, rendered)

    def _canonical_counts(self) -> tuple[int, int]:
        with sqlite3.connect(self.workspace.database_path) as connection:
            ledger = int(connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
            receipts = int(
                connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            )
        return ledger, receipts

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
