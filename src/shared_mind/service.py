"""Shared application service used by protocol-specific adapters.

The service accepts already-decoded values.  File loading, argument parsing,
JSON framing, and transport concerns belong to the CLI or another adapter.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .workspace import Workspace

if TYPE_CHECKING:
    from .query import QuerySpec


EXIT_OK = 0
EXIT_VALIDATION_ERROR = 3
EXIT_TRANSACTION_CONFLICT = 4
EXIT_INTERNAL_ERROR = 70


@dataclass(frozen=True)
class OperationResult:
    """One transport-neutral result and its adapter exit status."""

    ok: bool
    code: str
    data: Any | None = None
    errors: list[dict[str, Any]] | None = None
    message: str | None = None
    exit_code: int = EXIT_OK

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON envelope without the process exit status."""

        result: dict[str, Any] = {"ok": self.ok, "code": self.code}
        if self.message is not None:
            result["message"] = self.message
        if self.errors is not None:
            result["errors"] = _json_value(self.errors)
        if self.data is not None:
            result["data"] = _json_value(self.data)
        return result


class WorkspaceService:
    """Transport-neutral operations for one resolved workspace."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def validate_proposal(self, proposal: Any) -> OperationResult:
        issues = self.workspace.validate_proposal(proposal)
        if issues:
            return OperationResult(
                False,
                "PROPOSAL_INVALID",
                errors=[issue.as_dict() for issue in issues],
                message="Proposal validation failed.",
                exit_code=EXIT_VALIDATION_ERROR,
            )
        return OperationResult(True, "PROPOSAL_VALID", data={"valid": True})

    def commit_proposal(self, proposal: Any) -> OperationResult:
        kernel = self.workspace.open_kernel()
        try:
            receipt = kernel.commit(proposal)
            data = _receipt_data(receipt)
            if receipt.outcome == "TRANSACTION_CONFLICT":
                from .rebase import build_rebase_hint

                hint = build_rebase_hint(kernel, proposal, receipt)
                if hint is not None:
                    data["rebase_hint"] = hint
        finally:
            kernel.close()

        outcome = str(receipt.outcome)
        ok = outcome in ("COMMITTED", "FACT_CONFLICT")
        exit_code = {
            "COMMITTED": EXIT_OK,
            "FACT_CONFLICT": EXIT_OK,
            "TRANSACTION_CONFLICT": EXIT_TRANSACTION_CONFLICT,
            "VALIDATION_ERROR": EXIT_VALIDATION_ERROR,
        }.get(outcome, EXIT_INTERNAL_ERROR)
        return OperationResult(
            ok,
            outcome,
            data=data,
            message=None if ok else "Proposal was not committed.",
            exit_code=exit_code,
        )

    def query(self, spec: QuerySpec | Mapping[str, Any]) -> OperationResult:
        from .query import query

        kernel = self.workspace.open_kernel()
        try:
            try:
                query_result = query(kernel, spec)
            except (TypeError, ValueError) as exc:
                return OperationResult(
                    False,
                    "QUERY_INVALID",
                    message=str(exc),
                    exit_code=EXIT_VALIDATION_ERROR,
                )
        finally:
            kernel.close()
        return OperationResult(
            True,
            "QUERY_RESULTS",
            data=_json_value(query_result),
        )


def _receipt_data(receipt: Any) -> dict[str, Any]:
    data = {
        "proposal_id": receipt.proposal_id,
        "ledger_sequence": receipt.ledger_seq,
        "state_root": receipt.state_root,
        "reason_codes": list(receipt.reason_codes),
        "conflict_ids": list(receipt.conflict_ids),
    }
    document = getattr(receipt, "document", None)
    if isinstance(document, dict):
        data["decision_receipt"] = document
    return data


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _json_value(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


__all__ = ["OperationResult", "WorkspaceService"]
