from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_mind.cli import (
    EXIT_OK,
    EXIT_TRANSACTION_CONFLICT,
    EXIT_VALIDATION_ERROR,
    main,
)
from shared_mind.query import QUERY_VERSION, QuerySpec
from shared_mind.service import OperationResult, WorkspaceService
from shared_mind.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]


class Dev020ServiceAndCliContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temp.name) / "workspace"
        self.workspace = Workspace.initialize(
            self.workspace_root,
            purpose="Exercise the DEV-020 query and rebase contract.",
        )
        self.service = WorkspaceService(self.workspace)
        fixture_set = json.loads(
            (ROOT / "contracts" / "atlas-conformance-fixtures.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.objects = {
            item["name"]: item["object"] for item in fixture_set["typed_objects"]
        }
        self.source_content = (
            ROOT / "contracts" / "atlas-runbook.fixture.md"
        ).read_bytes()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_operation_result_has_one_shared_json_envelope(self) -> None:
        minimal = OperationResult(
            ok=True,
            code="READY",
            data=None,
            errors=None,
            message=None,
            exit_code=EXIT_OK,
        )
        failure = OperationResult(
            ok=False,
            code="QUERY_INVALID",
            data={"normalized_query": None},
            errors=[
                {
                    "code": "INVALID_QUERY",
                    "object_path": "$.limit",
                    "message": "limit must be between 1 and 1000",
                }
            ],
            message="Query validation failed.",
            exit_code=EXIT_VALIDATION_ERROR,
        )

        self.assertEqual({"ok": True, "code": "READY"}, minimal.as_dict())
        self.assertEqual(EXIT_OK, minimal.exit_code)
        self.assertEqual(
            {
                "ok": False,
                "code": "QUERY_INVALID",
                "message": "Query validation failed.",
                "errors": [
                    {
                        "code": "INVALID_QUERY",
                        "object_path": "$.limit",
                        "message": "limit must be between 1 and 1000",
                    }
                ],
                "data": {"normalized_query": None},
            },
            failure.as_dict(),
        )
        self.assertEqual(EXIT_VALIDATION_ERROR, failure.exit_code)

    def test_service_validate_accepts_inline_proposal_and_cli_alone_loads_file(
        self,
    ) -> None:
        proposal = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        with patch.object(
            Workspace,
            "load_json",
            side_effect=AssertionError("service must accept an inline proposal"),
        ) as forbidden_loader:
            service_result = self.service.validate_proposal(proposal)

        self.assertIsInstance(service_result, OperationResult)
        self.assertEqual(EXIT_OK, service_result.exit_code)
        self.assertEqual(
            {"ok": True, "code": "PROPOSAL_VALID", "data": {"valid": True}},
            service_result.as_dict(),
        )
        forbidden_loader.assert_not_called()

        proposal_path = self.workspace_root / "proposal.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        loaded_paths: list[Path] = []
        original_loader = Workspace.load_json

        def load_json_spy(workspace: Workspace, path: str | Path) -> object:
            loaded_paths.append(Path(path))
            return original_loader(workspace, path)

        with patch.object(Workspace, "load_json", new=load_json_spy):
            exit_code, cli_result, stderr = self.invoke(
                "proposal", "validate", str(proposal_path)
            )

        self.assertEqual(EXIT_OK, exit_code)
        self.assertEqual("", stderr)
        self.assertEqual([proposal_path], loaded_paths)
        self.assertEqual(service_result.as_dict(), cli_result)

    def test_service_commit_accepts_inline_proposal_and_cli_retry_has_same_receipt(
        self,
    ) -> None:
        proposal = self.seed_source()
        with patch.object(
            Workspace,
            "load_json",
            side_effect=AssertionError("service must not interpret proposal paths"),
        ) as forbidden_loader:
            service_result = self.service.commit_proposal(proposal)

        self.assertIsInstance(service_result, OperationResult)
        self.assertEqual(EXIT_OK, service_result.exit_code)
        self.assertEqual("COMMITTED", service_result.code)
        forbidden_loader.assert_not_called()
        receipt = service_result.data["decision_receipt"]
        self.assertEqual("DECISION_RECEIPT", receipt["object_type"])
        self.assertNotIn("rebase_hint", receipt)

        proposal_path = self.workspace_root / "commit.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        loaded_paths: list[Path] = []
        original_loader = Workspace.load_json

        def load_json_spy(workspace: Workspace, path: str | Path) -> object:
            loaded_paths.append(Path(path))
            return original_loader(workspace, path)

        with patch.object(Workspace, "load_json", new=load_json_spy):
            exit_code, cli_result, _ = self.invoke(
                "proposal", "commit", str(proposal_path)
            )

        self.assertEqual(EXIT_OK, exit_code)
        self.assertEqual([proposal_path], loaded_paths)
        self.assertEqual(service_result.as_dict(), cli_result)

    def test_query_service_accepts_query_spec_and_mapping_with_identical_results(
        self,
    ) -> None:
        self.seed_claim()
        spec = QuerySpec(
            kinds=("CLAIM",),
            ids=("claim_atlas_postgresql_001",),
            predicates=("deployment.database_engine@1",),
            statuses=("ACTIVE",),
            limit=10,
            offset=0,
            include_record=True,
        )
        mapping = {
            "kinds": ["CLAIM"],
            "ids": ["claim_atlas_postgresql_001"],
            "predicates": ["deployment.database_engine@1"],
            "statuses": ["ACTIVE"],
            "limit": 10,
            "offset": 0,
            "include_record": True,
        }

        from_spec = self.service.query(spec)
        from_mapping = self.service.query(mapping)

        self.assertIsInstance(from_spec, OperationResult)
        self.assertEqual(EXIT_OK, from_spec.exit_code)
        self.assertEqual("QUERY_RESULTS", from_spec.code)
        self.assertEqual(from_spec.as_dict(), from_mapping.as_dict())
        self.assertEqual(QUERY_VERSION, from_spec.data["query_version"])
        self.assertEqual(1, from_spec.data["total_matches"])
        self.assertFalse(from_spec.data["truncated"])
        self.assertEqual(
            ("CLAIM", "claim_atlas_postgresql_001"),
            (
                from_spec.data["hits"][0]["object_type"],
                from_spec.data["hits"][0]["object_id"],
            ),
        )
        self.assertIsInstance(from_spec.data["hits"][0]["record"], dict)

    def test_cli_query_without_filters_returns_the_same_deterministic_list(self) -> None:
        self.seed_claim()
        ledger_before = self.database_count("ledger")

        first_code, first, first_stderr = self.invoke("query")
        second_code, second, second_stderr = self.invoke("query")

        self.assertEqual(EXIT_OK, first_code)
        self.assertEqual(EXIT_OK, second_code)
        self.assertEqual("", first_stderr)
        self.assertEqual("", second_stderr)
        self.assertEqual("QUERY_RESULTS", first["code"])
        self.assertEqual(first, second)
        self.assertEqual(QUERY_VERSION, first["data"]["query_version"])
        self.assertEqual(
            first["data"]["total_matches"], len(first["data"]["hits"])
        )
        self.assertGreater(first["data"]["total_matches"], 0)
        self.assertFalse(first["data"]["truncated"])
        self.assertEqual(ledger_before, self.database_count("ledger"))

    def test_cli_query_supports_record_and_source_filters_and_summary_only(
        self,
    ) -> None:
        self.seed_claim()

        claim_code, claim_result, _ = self.invoke(
            "query",
            "--kind",
            "CLAIM",
            "--id",
            "claim_atlas_postgresql_001",
            "--predicate",
            "deployment.database_engine@1",
            "--status",
            "ACTIVE",
            "--limit",
            "10",
            "--offset",
            "0",
        )
        source_code, source_result, _ = self.invoke(
            "query",
            "--kind",
            "SOURCE_REVISION",
            "--id",
            "revision_atlas_runbook_20260801",
            "--title-contains",
            "production runbook",
            "--source-id",
            "document:atlas-runbook",
            "--source-revision-id",
            "revision_atlas_runbook_20260801",
            "--limit",
            "1",
            "--offset",
            "0",
            "--summary-only",
        )

        self.assertEqual(EXIT_OK, claim_code, claim_result)
        self.assertEqual("QUERY_RESULTS", claim_result["code"])
        self.assertEqual(1, claim_result["data"]["total_matches"])
        self.assertEqual(
            "claim_atlas_postgresql_001",
            claim_result["data"]["hits"][0]["object_id"],
        )
        self.assertIsInstance(claim_result["data"]["hits"][0]["record"], dict)

        self.assertEqual(EXIT_OK, source_code, source_result)
        self.assertEqual("QUERY_RESULTS", source_result["code"])
        self.assertEqual(1, source_result["data"]["total_matches"])
        self.assertEqual(
            "revision_atlas_runbook_20260801",
            source_result["data"]["hits"][0]["object_id"],
        )
        self.assertIsNone(source_result["data"]["hits"][0]["record"])

    def test_cli_query_invalid_filters_return_query_invalid(self) -> None:
        invalid_arguments = (
            ("--kind", "NOT_A_KIND"),
            ("--id", ""),
            ("--title-contains", ""),
            ("--predicate", ""),
            ("--source-id", ""),
            ("--source-revision-id", ""),
            ("--status", ""),
            ("--limit", "0"),
            ("--limit", "1001"),
            ("--offset", "-1"),
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                exit_code, result, _ = self.invoke("query", *arguments)

                self.assertEqual(EXIT_VALIDATION_ERROR, exit_code)
                self.assertFalse(result["ok"])
                self.assertEqual("QUERY_INVALID", result["code"])
                self.assertIn("message", result)

        service_result = self.service.query(
            {"kinds": ["CLAIM"], "unknown_filter": True}
        )
        self.assertIsInstance(service_result, OperationResult)
        self.assertEqual(EXIT_VALIDATION_ERROR, service_result.exit_code)
        self.assertFalse(service_result.ok)
        self.assertEqual("QUERY_INVALID", service_result.code)

    def test_transaction_conflict_adds_advisory_rebase_hint_without_mutating_receipt(
        self,
    ) -> None:
        self.seed_claim()
        attach_result = self.service.commit_proposal(self.attach_evidence_proposal())
        self.assertEqual("COMMITTED", attach_result.code)
        stale = copy.deepcopy(self.objects["stale_supersede_proposal"])
        expected_root, expected_head = self.current_root_and_head()

        result = self.service.commit_proposal(stale)

        self.assertIsInstance(result, OperationResult)
        self.assertFalse(result.ok)
        self.assertEqual("TRANSACTION_CONFLICT", result.code)
        self.assertEqual(EXIT_TRANSACTION_CONFLICT, result.exit_code)
        self.assertEqual(["CLAIM_VERSION_MISMATCH"], result.data["reason_codes"])
        receipt = result.data["decision_receipt"]
        hint = result.data["rebase_hint"]
        self.assertEqual(
            {
                "hint_version",
                "advisory",
                "proposal_id",
                "receipt_id",
                "reason_code",
                "observed_state_root",
                "observed_ledger_head",
                "failed_precondition",
                "replacement_preconditions",
                "safe_to_auto_apply",
                "recommended_action",
            },
            set(hint),
        )
        self.assertEqual("rebase-hint@1", hint["hint_version"])
        self.assertIs(True, hint["advisory"])
        self.assertEqual(stale["proposal_id"], hint["proposal_id"])
        self.assertEqual(receipt["receipt_id"], hint["receipt_id"])
        self.assertEqual("CLAIM_VERSION_MISMATCH", hint["reason_code"])
        self.assertEqual(expected_root, hint["observed_state_root"])
        self.assertEqual(expected_head, hint["observed_ledger_head"])
        self.assertEqual(receipt["head_after"], hint["observed_ledger_head"])
        failed = hint["failed_precondition"]
        self.assertEqual("$.reads[0].expected_version", failed["path"])
        self.assertEqual(1, failed["expected"])
        self.assertEqual(2, failed["actual"])
        self.assertEqual("CLAIM", failed["aggregate_type"])
        self.assertEqual("claim_atlas_postgresql_001", failed["aggregate_id"])
        self.assertEqual(
            {"version": 2, "status": "ACTIVE"}, failed["actual_state"]
        )
        replacement_by_path = {
            item["path"]: item["value"]
            for item in hint["replacement_preconditions"]
        }
        self.assertEqual(
            {
                "$.reads[0].expected_version": 2,
                "$.guards[0].expected_status": "ACTIVE",
                "$.guards[1].expected_version": 2,
            },
            replacement_by_path,
        )
        self.assertIs(False, hint["safe_to_auto_apply"])
        self.assertEqual("REVIEW_AND_REBUILD", hint["recommended_action"])
        self.assertNotIn("rebase_hint", receipt)
        self.assertEqual(self.persisted_receipt(stale["proposal_id"]), receipt)

        proposal_path = self.workspace_root / "stale.json"
        proposal_path.write_text(json.dumps(stale), encoding="utf-8")
        cli_code, cli_result, _ = self.invoke(
            "proposal", "commit", str(proposal_path)
        )
        self.assertEqual(EXIT_TRANSACTION_CONFLICT, cli_code)
        self.assertEqual(result.as_dict(), cli_result)

    def invoke(self, *arguments: str) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            ["--workspace", str(self.workspace_root), *arguments],
            stdout=stdout,
            stderr=stderr,
        )
        raw = stdout.getvalue()
        self.assertTrue(raw.endswith("\n"), raw)
        self.assertEqual(1, len(raw.splitlines()), raw)
        return exit_code, json.loads(raw), stderr.getvalue()

    def seed_source(self) -> dict[str, object]:
        kernel = self.workspace.open_kernel()
        try:
            receipt = kernel.register_source(
                copy.deepcopy(self.objects["source_revision_postgresql"]),
                self.source_content,
            )
        finally:
            kernel.close()
        self.assertEqual("COMMITTED", receipt.outcome)
        return copy.deepcopy(self.objects["assert_postgresql_proposal"])

    def seed_claim(self) -> None:
        result = self.service.commit_proposal(self.seed_source())
        self.assertEqual("COMMITTED", result.code)

    def attach_evidence_proposal(self) -> dict[str, object]:
        base = copy.deepcopy(self.objects["assert_postgresql_proposal"])
        evidence = copy.deepcopy(base["operations"][0]["initial_evidence"][0])
        evidence["evidence_link_id"] = "evidence_atlas_postgresql_extra"
        base["proposal_id"] = "proposal_attach_extra_evidence"
        base["idempotency_key"] = "atlas-attach-extra-001"
        base["operations"] = [
            {
                "op_id": "operation_attach_extra",
                "op": "ATTACH_EVIDENCE",
                "evidence_link": evidence,
            }
        ]
        return base

    def database_count(self, table: str) -> int:
        kernel = self.workspace.open_kernel()
        try:
            return int(
                kernel.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        finally:
            kernel.close()

    def current_root_and_head(self) -> tuple[str, str]:
        kernel = self.workspace.open_kernel()
        try:
            row = kernel.connection.execute(
                "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            return kernel.state_root(), str(row["entry_hash"])
        finally:
            kernel.close()

    def persisted_receipt(self, proposal_id: str) -> dict[str, object]:
        kernel = self.workspace.open_kernel()
        try:
            return next(
                receipt
                for receipt in kernel.decision_receipts()
                if receipt["proposal_id"] == proposal_id
            )
        finally:
            kernel.close()


if __name__ == "__main__":
    unittest.main()
