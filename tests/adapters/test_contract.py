from __future__ import annotations

import dataclasses
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from shared_mind.adapters import (
    AdapterFailure,
    AdapterSnapshot,
    AdapterSource,
    ReviewedMapping,
    adapter_catalog,
    create_adapter,
    run_import,
)
from shared_mind.service import WorkspaceService
from shared_mind.workspace import Workspace

from tests.adapters.support import (
    RecordingAdapter,
    canonical_store_snapshot,
    fixture_catalog,
    source_for,
)


class ExternalAdapterCatalogContractTest(unittest.TestCase):
    def test_catalog_is_pinned_and_matches_reviewed_fixture(self) -> None:
        expected = fixture_catalog()

        actual = adapter_catalog()

        self.assertEqual("external-adapter-contract@1", actual.contract_version)
        self.assertEqual(
            expected["adapters"],
            [spec.as_dict() for spec in actual.adapters],
        )

    def test_catalog_restricts_all_adapters_to_source_only_by_default(self) -> None:
        for adapter_spec in adapter_catalog().adapters:
            with self.subTest(adapter=adapter_spec.name):
                self.assertTrue(adapter_spec.source_only_default)
                self.assertEqual(
                    ("SOURCE_REVISION",), adapter_spec.allowed_outputs
                )
                self.assertEqual(
                    "REVIEWED_MAPPING_ONLY", adapter_spec.semantic_promotion
                )

    def test_upstream_specific_contracts_are_not_silently_widened(self) -> None:
        specs = {item.name: item for item in adapter_catalog().adapters}

        self.assertEqual("stable-event-json", specs["qarinah"].stability)
        self.assertEqual(("EVENT_JSON",), specs["qarinah"].allowed_inputs)
        self.assertEqual(
            "restricted-citation-import", specs["atomicstrata"].stability
        )
        self.assertEqual(
            ("JSON_EXPORT", "OKF_REFERENCE"),
            specs["atomicstrata"].allowed_inputs,
        )
        self.assertEqual("3.21.0", specs["swarmvault"].upstream_version)
        self.assertEqual(
            "815412d24298e59e5073ded1ddd6c0e6aee9b91b",
            specs["swarmvault"].upstream_pin,
        )
        self.assertEqual(
            "provisional-source-context-only", specs["swarmvault"].stability
        )

    def test_adapter_constructors_accept_bytes_not_workspace_or_database_paths(self) -> None:
        signature = inspect.signature(create_adapter)

        self.assertEqual(("name", "source"), tuple(signature.parameters))
        self.assertNotIn("workspace", signature.parameters)
        self.assertNotIn("database", signature.parameters)
        self.assertNotIn("db_path", signature.parameters)


class ImmutableAdapterInputContractTest(unittest.TestCase):
    def test_source_bytes_are_mandatory(self) -> None:
        with self.assertRaisesRegex((TypeError, ValueError), "content|bytes"):
            AdapterSource(
                locator="fixture://missing.json",
                media_type="application/json",
                content=None,  # type: ignore[arg-type]
            )

    def test_mutable_source_content_is_rejected(self) -> None:
        with self.assertRaisesRegex((TypeError, ValueError), "content|bytes"):
            AdapterSource(
                locator="fixture://mutable.json",
                media_type="application/json",
                content=bytearray(b"{}"),  # type: ignore[arg-type]
            )

    def test_source_and_snapshot_are_frozen_and_content_addressed(self) -> None:
        source = source_for("qarinah")
        adapter = create_adapter("qarinah", source)

        probe = adapter.probe()
        snapshot = adapter.snapshot(probe)

        self.assertIsInstance(snapshot, AdapterSnapshot)
        self.assertEqual(source.content, snapshot.content)
        self.assertRegex(snapshot.content_hash, r"^sha256:[0-9a-f]{64}$")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.content_hash = "sha256:" + "0" * 64  # type: ignore[misc]


class AdapterLifecycleContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace.initialize(Path(self.temp.name) / "workspace")
        self.service = WorkspaceService(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_flow_is_probe_snapshot_validate_plan_validate_commit(self) -> None:
        adapter = RecordingAdapter()

        result = run_import(adapter, self.service)

        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual("COMMITTED", result.code)
        self.assertEqual(
            ["PROBE", "SNAPSHOT", "VALIDATE", "PLAN", "PLAN"],
            [stage for stage, _ in adapter.calls],
        )
        self.assertEqual(1, canonical_store_snapshot(self.workspace)["ledger"])

    def test_runner_passes_only_probe_snapshot_and_reviewed_mapping_to_adapter(self) -> None:
        adapter = RecordingAdapter()

        result = run_import(adapter, self.service)

        self.assertTrue(result.ok, result.as_dict())
        calls = dict(adapter.calls)
        self.assertEqual((), calls["PROBE"])
        self.assertEqual(1, len(calls["SNAPSHOT"]))
        self.assertEqual(1, len(calls["VALIDATE"]))
        self.assertEqual(2, len(calls["PLAN"]))
        self.assertIsNone(calls["PLAN"][1])
        flattened = repr(adapter.calls)
        self.assertNotIn(str(self.workspace.database_path), flattened)
        self.assertNotIn(str(self.workspace.root), flattened)

    def test_planner_must_be_deterministic_before_commit(self) -> None:
        adapter = RecordingAdapter(nondeterministic_plan=True)
        before = canonical_store_snapshot(self.workspace)

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_NONDETERMINISTIC_PLAN", result.code)
        self.assertEqual(before, canonical_store_snapshot(self.workspace))

    def test_snapshot_content_hash_is_recomputed_before_plan(self) -> None:
        adapter = RecordingAdapter(corrupt_snapshot_hash=True)
        before = canonical_store_snapshot(self.workspace)

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_SNAPSHOT_HASH_MISMATCH", result.code)
        self.assertNotIn("PLAN", [stage for stage, _ in adapter.calls])
        self.assertEqual(before, canonical_store_snapshot(self.workspace))

    def test_one_proposal_cannot_exceed_128_operations(self) -> None:
        adapter = RecordingAdapter(operation_count=129)
        before = canonical_store_snapshot(self.workspace)

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_OPERATION_LIMIT_EXCEEDED", result.code)
        self.assertEqual(before, canonical_store_snapshot(self.workspace))

    def test_semantic_auto_promotion_is_rejected_without_reviewed_mapping(self) -> None:
        adapter = RecordingAdapter(semantic=True)
        before = canonical_store_snapshot(self.workspace)

        result = run_import(adapter, self.service)

        self.assertFalse(result.ok)
        self.assertEqual("ADAPTER_REVIEWED_MAPPING_REQUIRED", result.code)
        self.assertEqual(before, canonical_store_snapshot(self.workspace))

    def test_reviewed_mapping_is_explicit_versioned_and_narrow(self) -> None:
        mapping = ReviewedMapping(
            mapping_id="mapping_atomicstrata_claims_001",
            mapping_version="1.0.0",
            reviewed_by="human:maintainer",
            reviewed_at="2026-08-11T00:00:00Z",
            allowed_operations=("REGISTER_SOURCE_REVISION", "ASSERT_CLAIM"),
        )
        adapter = RecordingAdapter(semantic=True)

        result = run_import(adapter, self.service, mapping=mapping)

        # The reviewed mapping crosses only the adapter policy boundary.  The
        # kernel remains authoritative and rejects the fixture's absent source.
        self.assertFalse(result.ok)
        self.assertNotEqual("ADAPTER_REVIEWED_MAPPING_REQUIRED", result.code)
        self.assertEqual(mapping, adapter.calls[-1][1][1])

    def test_vendor_adapters_create_deterministic_source_only_imports(self) -> None:
        results: dict[str, tuple[str, str, str]] = {}
        for name in ("qarinah", "atomicstrata", "swarmvault"):
            with self.subTest(adapter=name):
                workspace = Workspace.initialize(
                    Path(self.temp.name) / f"workspace-{name}"
                )
                adapter = create_adapter(name, source_for(name))

                result = run_import(adapter, WorkspaceService(workspace))

                self.assertTrue(result.ok, result.as_dict())
                kernel = workspace.open_kernel()
                try:
                    proposal = kernel.connection.execute(
                        "SELECT proposal FROM ledger WHERE seq = 1"
                    ).fetchone()["proposal"]
                    source = kernel.connection.execute(
                        "SELECT revision_id, content_hash FROM sources"
                    ).fetchone()
                    results[name] = (
                        result.data["proposal_id"],
                        source["revision_id"],
                        source["content_hash"],
                    )
                    self.assertIn('"op":"REGISTER_SOURCE_REVISION"', proposal)
                    self.assertNotIn('"op":"ASSERT_CLAIM"', proposal)
                finally:
                    kernel.close()

        self.assertEqual(3, len(set(results.values())))

    def test_unknown_or_unpinned_adapter_is_rejected(self) -> None:
        with self.assertRaises(AdapterFailure) as caught:
            create_adapter("unknown", source_for("qarinah"))

        self.assertEqual("ADAPTER_NOT_SUPPORTED", caught.exception.code)

    def test_qarinah_requires_the_pinned_stable_event_json_shape(self) -> None:
        source = AdapterSource(
            locator="fixture://bad-qarinah.json",
            media_type="application/json",
            content=b'{"event_type":"memory.created","payload":{}}',
        )
        adapter = create_adapter("qarinah", source)
        snapshot = adapter.snapshot(adapter.probe())

        with self.assertRaises(AdapterFailure) as caught:
            adapter.validate(snapshot)

        self.assertEqual("QARINAH_EVENT_INVALID", caught.exception.code)

    def test_atomicstrata_citations_require_resolved_okf_references(self) -> None:
        document = json.loads(source_for("atomicstrata").content)
        document["okf_references"] = []
        source = AdapterSource(
            locator="fixture://bad-atomicstrata.json",
            media_type="application/json",
            content=json.dumps(document, sort_keys=True).encode("utf-8"),
        )
        adapter = create_adapter("atomicstrata", source)
        snapshot = adapter.snapshot(adapter.probe())

        with self.assertRaises(AdapterFailure) as caught:
            adapter.validate(snapshot)

        self.assertEqual(
            "ATOMICSTRATA_OKF_REFERENCE_REQUIRED", caught.exception.code
        )

    def test_swarmvault_rejects_unpinned_or_graph_only_exports(self) -> None:
        document = json.loads(source_for("swarmvault").content)
        document["swarmvault_version"] = "3.22.0"
        source = AdapterSource(
            locator="fixture://bad-swarmvault.json",
            media_type="application/json",
            content=json.dumps(document, sort_keys=True).encode("utf-8"),
        )
        adapter = create_adapter("swarmvault", source)
        snapshot = adapter.snapshot(adapter.probe())

        with self.assertRaises(AdapterFailure) as caught:
            adapter.validate(snapshot)

        self.assertEqual("SWARMVAULT_VERSION_MISMATCH", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
