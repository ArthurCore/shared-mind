from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest import mock

from shared_mind.adapters import (
    AdapterFailure,
    AdapterProbe,
    AdapterSnapshot,
    AdapterSource,
    create_adapter,
    run_import,
)
from shared_mind.canonical import sha256_bytes, sha256_json
from shared_mind.kernel import Kernel
from shared_mind.service import WorkspaceService
from shared_mind.workspace import Workspace, WorkspaceError

from tests.adapters.support import RecordingAdapter, source_for


MAX_SOURCE_BYTES = 1024 * 1024


class InputResourceHardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace.initialize(Path(self.temp.name) / "workspace")
        self.service = WorkspaceService(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_oversized_source_fails_before_blob_receipt_or_ledger(self) -> None:
        source = self.workspace.source_root / "oversized.md"
        source.write_bytes(b"x" * (MAX_SOURCE_BYTES + 1))
        before = self._store_snapshot()

        with self.assertRaises(WorkspaceError) as caught:
            self.workspace.add_source(source, source_id="document:oversized")

        self.assertEqual("SOURCE_TOO_LARGE", caught.exception.code)
        self.assertEqual(before, self._store_snapshot())

    def test_source_proposal_canonical_overhead_fails_before_blob(self) -> None:
        # 3/4 of the proposal cap expands to a full cap of base64 characters;
        # the canonical proposal envelope necessarily pushes it over the limit.
        raw_size = Kernel.MAX_PROPOSAL_BYTES * 3 // 4
        self.assertLess(raw_size, MAX_SOURCE_BYTES)
        source = self.workspace.source_root / "base64-overhead.md"
        source.write_bytes(b"x" * raw_size)
        before = self._store_snapshot()

        with self.assertRaises(WorkspaceError) as caught:
            self.workspace.add_source(source, source_id="document:base64-overhead")

        self.assertEqual("SOURCE_TOO_LARGE", caught.exception.code)
        self.assertEqual(before, self._store_snapshot())

    def test_validate_proposal_rejects_inline_canonical_size_without_receipt(
        self,
    ) -> None:
        proposal = {
            "object_type": "PROPOSAL",
            "proposal_id": "proposal_validate_oversized_001",
            "idempotency_key": "validate-oversized-001",
            "padding": "x" * Kernel.MAX_PROPOSAL_BYTES,
        }
        before = self._store_snapshot()

        result = self.service.validate_proposal(proposal)

        self.assertFalse(result.ok)
        self.assertEqual("PROPOSAL_INVALID", result.code)
        self.assertEqual(
            ["PROPOSAL_TOO_LARGE"],
            [error["code"] for error in result.errors or []],
        )
        self.assertEqual(before, self._store_snapshot())

    def test_validate_proposal_rejects_inline_depth_without_receipt(self) -> None:
        nested: object = 0
        for _ in range(Kernel.MAX_PROPOSAL_DEPTH + 1):
            nested = {"nested": nested}
        proposal = {
            "object_type": "PROPOSAL",
            "proposal_id": "proposal_validate_too_deep_001",
            "idempotency_key": "validate-too-deep-001",
            "padding": nested,
        }
        before = self._store_snapshot()

        result = self.service.validate_proposal(proposal)

        self.assertFalse(result.ok)
        self.assertEqual("PROPOSAL_INVALID", result.code)
        self.assertEqual(
            ["PROPOSAL_TOO_DEEP"],
            [error["code"] for error in result.errors or []],
        )
        self.assertEqual(before, self._store_snapshot())

    def test_adapter_source_rejects_oversized_immutable_bytes(self) -> None:
        with self.assertRaises(AdapterFailure) as caught:
            AdapterSource(
                locator="fixture://oversized.json",
                media_type="application/json",
                content=b"x" * (MAX_SOURCE_BYTES + 1),
            )

        self.assertEqual("ADAPTER_SOURCE_TOO_LARGE", caught.exception.code)
        self.assertEqual("CREATE", caught.exception.stage)

    def test_run_import_stops_oversized_snapshot_before_plan_or_service(self) -> None:
        class OversizedSnapshotAdapter(RecordingAdapter):
            def snapshot(self, probe: AdapterProbe) -> AdapterSnapshot:
                self._record("SNAPSHOT", probe)
                content = b"x" * (MAX_SOURCE_BYTES + 1)
                return AdapterSnapshot(
                    adapter_name=self.spec.name,
                    upstream_version=self.spec.upstream_version,
                    source_locator="fixture://oversized.json",
                    media_type="application/json",
                    content=content,
                    content_hash=sha256_bytes(content),
                )

        adapter = OversizedSnapshotAdapter()
        before = self._store_snapshot()

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_SOURCE_TOO_LARGE", result.code)
        self.assertEqual("SNAPSHOT", result.data["stage"])
        self.assertEqual(
            ["PROBE", "SNAPSHOT"], [stage for stage, _ in adapter.calls]
        )
        self.assertEqual(before, self._store_snapshot())

    def test_service_exposes_current_versions_without_storage_paths(self) -> None:
        expected = self._custom_registry()
        custom_workspace = self._custom_registry_workspace(expected)

        versions = WorkspaceService(custom_workspace).current_version_bundle()

        self.assertEqual(
            {
                "schema": Kernel.SUPPORTED_VERSIONS["schema"],
                "predicate_registry": expected["version"],
                "predicate_registry_hash": sha256_json(expected),
                "conflict_rules": Kernel.SUPPORTED_VERSIONS["conflict_rules"],
                "guard_dsl": expected["guard_dsl_version"],
                "projection": Kernel.SUPPORTED_VERSIONS["projection"],
            },
            versions,
        )
        rendered = json.dumps(versions, sort_keys=True)
        self.assertNotIn(str(custom_workspace.root), rendered)
        self.assertNotIn(str(custom_workspace.database_path), rendered)

    def test_builtin_adapters_pin_the_custom_workspace_registry(self) -> None:
        registry = self._custom_registry()
        custom_workspace = self._custom_registry_workspace(registry)
        service = WorkspaceService(custom_workspace)

        for name in ("qarinah", "atomicstrata", "swarmvault"):
            with self.subTest(adapter=name):
                result = run_import(create_adapter(name, source_for(name)), service)
                self.assertTrue(result.ok, result.as_dict())

        expected_versions = service.current_version_bundle()
        kernel = custom_workspace.open_kernel()
        try:
            proposals = [
                json.loads(row[0])
                for row in kernel.connection.execute(
                    "SELECT proposal FROM ledger ORDER BY seq"
                ).fetchall()
            ]
        finally:
            kernel.close()
        self.assertEqual(3, len(proposals))
        self.assertTrue(
            all(proposal["versions"] == expected_versions for proposal in proposals)
        )

    @unittest.skipUnless(
        os.open in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW"),
        "secure dir_fd walking is unavailable",
    )
    def test_intermediate_source_directory_swap_is_blocked_by_dirfd_walk(
        self,
    ) -> None:
        self._assert_intermediate_source_swap_is_blocked(force_fallback=False)

    def test_intermediate_source_directory_swap_is_blocked_by_fallback(
        self,
    ) -> None:
        self._assert_intermediate_source_swap_is_blocked(force_fallback=True)

    def test_existing_blob_symlink_is_never_followed_or_accepted(self) -> None:
        content = b"same bytes must not make an external symlink acceptable\n"
        source = self.workspace.source_root / "blob-symlink.md"
        source.write_bytes(content)
        outside = Path(self.temp.name) / "outside-blob.bin"
        outside.write_bytes(content)
        blob = self.workspace.blob_root / sha256_bytes(content).split(":", 1)[1]
        try:
            os.symlink(outside, blob)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are not available")
        before = self._store_snapshot()

        with self.assertRaises(WorkspaceError) as caught:
            self.workspace.add_source(source, source_id="document:blob-symlink")

        self.assertIn(
            caught.exception.code, {"BLOB_INTEGRITY_ERROR", "BLOB_READ_FAILED"}
        )
        self.assertEqual(before, self._store_snapshot())
        self.assertTrue(blob.is_symlink())

    def _store_snapshot(self) -> dict[str, Any]:
        connection = sqlite3.connect(self.workspace.database_path)
        try:
            ledger = int(connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
            receipts = int(
                connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            )
            sources = int(
                connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            )
        finally:
            connection.close()
        kernel = self.workspace.open_kernel()
        try:
            state_root = kernel.state_root()
        finally:
            kernel.close()
        blobs = tuple(
            sorted(
                path.name
                for path in self.workspace.blob_root.iterdir()
                if path.is_file()
            )
        )
        return {
            "ledger": ledger,
            "receipts": receipts,
            "sources": sources,
            "state_root": state_root,
            "blobs": blobs,
        }

    def _assert_intermediate_source_swap_is_blocked(
        self, *, force_fallback: bool
    ) -> None:
        nested = self.workspace.source_root / "nested"
        nested.mkdir()
        source = nested / "race.md"
        source.write_text("trusted bytes\n", encoding="utf-8")
        outside = Path(self.temp.name) / "outside-directory"
        outside.mkdir()
        (outside / source.name).write_text(
            "outside secret must never be read\n", encoding="utf-8"
        )
        parked = self.workspace.source_root / "nested-parked"
        before = self._store_snapshot()
        original_resolve = Workspace.resolve_source_input

        def resolve_then_swap(workspace: Workspace, candidate: object) -> Path:
            resolved = original_resolve(workspace, candidate)  # type: ignore[arg-type]
            nested.rename(parked)
            try:
                os.symlink(outside, nested, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("directory symlinks are not available")
            return resolved

        fd_root = Path("/dev/fd")
        fd_before = len(tuple(fd_root.iterdir())) if fd_root.is_dir() else None
        fallback = mock.patch(
            "shared_mind.workspace._SUPPORTS_SECURE_DIR_FD",
            False,
            create=True,
        )
        fallback_context = fallback if force_fallback else nullcontext()
        with mock.patch.object(
            Workspace, "resolve_source_input", new=resolve_then_swap
        ), fallback_context:
            with self.assertRaises(WorkspaceError) as caught:
                self.workspace.add_source(source, source_id="document:race")

        self.assertIn(
            caught.exception.code,
            {"PATH_OUTSIDE_SOURCE_ROOT", "SOURCE_READ_FAILED"},
        )
        self.assertEqual(before, self._store_snapshot())
        if fd_before is not None:
            self.assertEqual(fd_before, len(tuple(fd_root.iterdir())))
        self.assertEqual(
            b"outside secret must never be read\n",
            (outside / source.name).read_bytes(),
        )

    @staticmethod
    def _custom_registry() -> dict[str, Any]:
        registry_path = (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "atlas-predicate-registry.v1.json"
        )
        registry = json.loads(
            registry_path.read_text(encoding="utf-8")
        )
        registry["version"] = "1.1.0"
        registry["guard_dsl_version"] = "guard-dsl@2"
        return registry

    def _custom_registry_workspace(self, registry: dict[str, Any]) -> Workspace:
        registry_path = Path(self.temp.name) / "custom-registry.json"
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return Workspace.initialize(
            Path(self.temp.name) / "custom-workspace",
            registry_source=registry_path,
        )


if __name__ == "__main__":
    unittest.main()
