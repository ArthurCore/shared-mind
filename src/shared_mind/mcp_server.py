"""Local MCP adapter over the transport-neutral Shared Mind service.

The dispatcher and resource allowlist in this module have no MCP SDK
dependency.  ``create_server`` imports the optional SDK only when a stdio
transport is actually requested.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .projection import ContextBudgetError, build_context_pack, project_json, project_markdown
from .service import OperationResult, WorkspaceService
from .validation import load_default_schema
from .workspace import Workspace, WorkspaceError


TOOL_NAMES = (
    "context",
    "query",
    "proposal_validate",
    "proposal_commit",
    "source_add",
    "conflict_list",
    "ledger_verify",
)

RESOURCE_URIS = (
    "shared-mind://workspace/info",
    "shared-mind://workspace/context",
    "shared-mind://projection/project.json",
    "shared-mind://projection/project.md",
    "shared-mind://contract/schema",
    "shared-mind://contract/predicate-registry",
)

_OPERATION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "code": {"type": "string"},
        "data": {},
        "errors": {"type": "array", "items": {"type": "object"}},
        "message": {"type": "string"},
    },
    "required": ["ok", "code"],
    "additionalProperties": False,
}


def _object_schema(
    properties: Mapping[str, Any] | None = None,
    *,
    required: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }


_ARRAY_OF_STRINGS = {"type": "array", "items": {"type": "string", "minLength": 1}}

_TOOL_DEFINITIONS = (
    {
        "name": "context",
        "description": "Build a deterministic, budgeted handoff context pack.",
        "inputSchema": _object_schema(
            {
                "budget_bytes": {"type": "integer", "minimum": 1},
                "budget_tokens": {"type": "integer", "minimum": 1},
            }
        ),
        "outputSchema": _OPERATION_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "query",
        "description": "Query the deterministic public projection.",
        "inputSchema": _object_schema(
            {
                "kinds": _ARRAY_OF_STRINGS,
                "ids": _ARRAY_OF_STRINGS,
                "title_contains": {"type": "string", "minLength": 1},
                "predicates": _ARRAY_OF_STRINGS,
                "source_ids": _ARRAY_OF_STRINGS,
                "source_revision_ids": _ARRAY_OF_STRINGS,
                "statuses": _ARRAY_OF_STRINGS,
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "offset": {"type": "integer", "minimum": 0},
                "summary_only": {"type": "boolean"},
            }
        ),
        "outputSchema": _OPERATION_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "proposal_validate",
        "description": "Validate one inline Proposal without mutating canonical state.",
        "inputSchema": _object_schema(
            {"proposal": {"type": "object"}}, required=("proposal",)
        ),
        "outputSchema": _OPERATION_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "proposal_commit",
        "description": "Submit one inline Proposal to the deterministic kernel.",
        "inputSchema": _object_schema(
            {"proposal": {"type": "object"}}, required=("proposal",)
        ),
        "outputSchema": _OPERATION_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "source_add",
        "description": "Register a UTF-8 source by path relative to the source root.",
        "inputSchema": _object_schema(
            {
                "path": {"type": "string", "minLength": 1},
                "source_id": {"type": "string", "minLength": 1},
            },
            required=("path",),
        ),
        "outputSchema": _OPERATION_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "conflict_list",
        "description": "List canonical conflicts, optionally filtered by status.",
        "inputSchema": _object_schema(
            {
                "status": {
                    "type": "string",
                    "enum": ["OPEN", "RESOLVED", "REOPENED"],
                }
            }
        ),
        "outputSchema": _OPERATION_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "ledger_verify",
        "description": "Verify the canonical ledger chain and materialized state root.",
        "inputSchema": _object_schema(),
        "outputSchema": _OPERATION_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
)

_RESOURCE_DEFINITIONS = (
    {
        "uri": "shared-mind://workspace/info",
        "name": "Workspace information",
        "description": "Resolved local workspace metadata.",
        "mimeType": "application/json",
    },
    {
        "uri": "shared-mind://workspace/context",
        "name": "Handoff context",
        "description": "Default deterministic handoff context pack.",
        "mimeType": "application/json",
    },
    {
        "uri": "shared-mind://projection/project.json",
        "name": "JSON projection",
        "description": "Lossless deterministic JSON projection.",
        "mimeType": "application/json",
    },
    {
        "uri": "shared-mind://projection/project.md",
        "name": "Markdown projection",
        "description": "Human-reviewable deterministic Markdown projection.",
        "mimeType": "text/markdown",
    },
    {
        "uri": "shared-mind://contract/schema",
        "name": "Kernel contract schema",
        "description": "Pinned Shared Mind kernel JSON Schema.",
        "mimeType": "application/json",
    },
    {
        "uri": "shared-mind://contract/predicate-registry",
        "name": "Predicate registry",
        "description": "Workspace-pinned predicate registry.",
        "mimeType": "application/json",
    },
)


@dataclass
class _SdkOperationEnvelope:
    """Return annotation from which FastMCP derives the public output schema."""

    ok: bool
    code: str
    data: Any | None = None
    errors: list[dict[str, Any]] | None = None
    message: str | None = None


class McpApplication:
    """Dependency-free dispatcher bound to one immutable workspace selection."""

    def __init__(self, workspace: Workspace) -> None:
        if not isinstance(workspace, Workspace):
            raise TypeError("workspace must be a resolved Workspace")
        self.workspace = workspace
        self.service = WorkspaceService(workspace)

    def list_tools(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(_TOOL_DEFINITIONS))

    def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        definition = next(
            (item for item in _TOOL_DEFINITIONS if item["name"] == name), None
        )
        if definition is None:
            return _tool_result(
                OperationResult(
                    False,
                    "MCP_TOOL_NOT_FOUND",
                    message=f"Unknown Shared Mind MCP tool: {name}",
                    exit_code=2,
                )
            )
        if arguments is None:
            values: dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            values = dict(arguments)
        else:
            return _tool_result(_argument_error("Tool arguments must be an object."))
        schema = definition["inputSchema"]
        unknown = sorted(set(values) - set(schema["properties"]))
        missing = [key for key in schema["required"] if key not in values]
        if unknown:
            return _tool_result(
                _argument_error("Unknown tool argument(s): " + ", ".join(unknown))
            )
        if missing:
            return _tool_result(
                _argument_error("Missing tool argument(s): " + ", ".join(missing))
            )
        try:
            result = self._dispatch(name, values)
        except WorkspaceError as exc:
            result = OperationResult(
                False, exc.code, message=exc.message, exit_code=3
            )
        except (TypeError, ValueError) as exc:
            result = _argument_error(str(exc))
        return _tool_result(result)

    def list_resources(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(_RESOURCE_DEFINITIONS))

    def read_resource(self, uri: str) -> dict[str, list[dict[str, str]]]:
        descriptor = next(
            (item for item in _RESOURCE_DEFINITIONS if item["uri"] == uri), None
        )
        if descriptor is None:
            raise ValueError(f"Unknown Shared Mind resource URI: {uri}")
        text = self._resource_text(uri)
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": descriptor["mimeType"],
                    "text": text,
                }
            ]
        }

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> OperationResult:
        if name == "proposal_validate":
            proposal = arguments["proposal"]
            if not isinstance(proposal, Mapping):
                raise TypeError("proposal must be an object")
            return self.service.validate_proposal(dict(proposal))
        if name == "proposal_commit":
            proposal = arguments["proposal"]
            if not isinstance(proposal, Mapping):
                raise TypeError("proposal must be an object")
            return self.service.commit_proposal(dict(proposal))
        if name == "query":
            spec = dict(arguments)
            summary_only = spec.pop("summary_only", False)
            if not isinstance(summary_only, bool):
                raise TypeError("summary_only must be a boolean")
            spec["include_record"] = not summary_only
            return self.service.query(spec)
        if name == "source_add":
            return self._source_add(arguments)
        if name == "context":
            return self._context(arguments)
        if name == "conflict_list":
            return self._conflict_list(arguments)
        if name == "ledger_verify":
            return self._ledger_verify()
        raise AssertionError(f"Unregistered tool dispatch: {name}")

    def _source_add(self, arguments: dict[str, Any]) -> OperationResult:
        source_path = arguments["path"]
        if not isinstance(source_path, str) or not source_path:
            raise TypeError("path must be a non-empty relative string")
        relative_path = Path(source_path)
        if relative_path.is_absolute():
            raise WorkspaceError(
                "PATH_OUTSIDE_SOURCE_ROOT",
                "MCP source paths must be relative to the workspace source root.",
            )
        source_id = arguments.get("source_id")
        if source_id is not None and not isinstance(source_id, str):
            raise TypeError("source_id must be a string")
        source, receipt = self.workspace.add_source(
            self.workspace.source_root / relative_path,
            source_id=source_id,
        )
        receipt_data = _receipt_data(receipt)
        if receipt.outcome in ("COMMITTED", "FACT_CONFLICT"):
            data = dict(source)
            data.update(receipt_data)
            return OperationResult(True, "SOURCE_REGISTERED", data=data)
        return _receipt_result(receipt, receipt_data)

    def _context(self, arguments: dict[str, Any]) -> OperationResult:
        keyword_arguments: dict[str, Any] = {}
        for name in ("budget_bytes", "budget_tokens"):
            value = arguments.get(name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
                keyword_arguments[name] = value
        if self.workspace.purpose is not None:
            keyword_arguments["purpose"] = self.workspace.purpose
        kernel = self.workspace.open_kernel()
        try:
            try:
                context = build_context_pack(kernel, **keyword_arguments)
            except ContextBudgetError as exc:
                return OperationResult(
                    False,
                    "CONTEXT_BUDGET_TOO_SMALL",
                    data={
                        "required_bytes": exc.required_bytes,
                        "budget_bytes": exc.budget_bytes,
                    },
                    message=str(exc),
                    exit_code=3,
                )
        finally:
            kernel.close()
        return OperationResult(
            True,
            "CONTEXT_READY",
            data={
                "context": context,
                "filters": {"project": None, "subject": None},
            },
        )

    def _conflict_list(self, arguments: dict[str, Any]) -> OperationResult:
        status = arguments.get("status")
        if status is not None and status not in ("OPEN", "RESOLVED", "REOPENED"):
            raise ValueError("status must be OPEN, RESOLVED, or REOPENED")
        conflicts = self.workspace.list_conflicts(status)
        return OperationResult(
            True,
            "CONFLICTS_LISTED",
            data={"conflicts": conflicts, "count": len(conflicts)},
        )

    def _ledger_verify(self) -> OperationResult:
        kernel = self.workspace.open_kernel()
        try:
            report = kernel.verify_ledger()
        finally:
            kernel.close()
        valid = bool(report.get("valid"))
        return OperationResult(
            valid,
            "LEDGER_VALID" if valid else "LEDGER_INVALID",
            data=report,
            message=None if valid else "Ledger verification failed.",
            exit_code=0 if valid else 5,
        )

    def _resource_text(self, uri: str) -> str:
        if uri == "shared-mind://workspace/info":
            return canonical_json(self.workspace.describe()) + "\n"
        if uri == "shared-mind://contract/schema":
            return canonical_json(load_default_schema()) + "\n"
        if uri == "shared-mind://contract/predicate-registry":
            document = json.loads(self.workspace.registry_path.read_text(encoding="utf-8"))
            return canonical_json(document) + "\n"
        kernel = self.workspace.open_kernel()
        try:
            if uri == "shared-mind://workspace/context":
                keyword_arguments = (
                    {"purpose": self.workspace.purpose}
                    if self.workspace.purpose is not None
                    else {}
                )
                return canonical_json(
                    build_context_pack(kernel, **keyword_arguments)
                ) + "\n"
            if uri == "shared-mind://projection/project.json":
                return project_json(kernel)
            if uri == "shared-mind://projection/project.md":
                return project_markdown(kernel)
        finally:
            kernel.close()
        raise AssertionError(f"Unregistered resource URI: {uri}")


def create_server(workspace: Workspace) -> Any:
    """Create an SDK-backed server while keeping the base package optional."""

    from mcp.types import ToolAnnotations

    try:
        # MCP Python SDK v2 renamed the high-level server and removed the old
        # module instead of leaving a compatibility alias.
        from mcp.server import MCPServer

        server_type = MCPServer
        sdk_v2 = True
    except ImportError:  # pragma: no cover - exercised with the v1 SDK
        from mcp.server.fastmcp import FastMCP

        server_type = FastMCP
        sdk_v2 = False

    application = McpApplication(workspace)
    server = server_type(
        "shared-mind",
        instructions=(
            "Local-only Shared Mind adapter. Read context and projections, then "
            "submit inline proposals for deterministic validation or commit."
        ),
    )

    def sdk_result(result: dict[str, Any]) -> dict[str, Any]:
        envelope = result["structuredContent"]
        if result["isError"]:
            # Both SDK generations convert an ordinary exception raised by a
            # high-level tool into a model-visible isError result. Avoid an
            # SDK-private exception import, which moved in v2.
            raise RuntimeError(canonical_json(envelope))
        return envelope

    def annotations(name: str) -> Any:
        definition = next(item for item in _TOOL_DEFINITIONS if item["name"] == name)
        hints = definition["annotations"]
        if sdk_v2:
            return ToolAnnotations(
                read_only_hint=hints["readOnlyHint"],
                destructive_hint=hints["destructiveHint"],
                idempotent_hint=hints["idempotentHint"],
                open_world_hint=hints["openWorldHint"],
            )
        return ToolAnnotations(
            readOnlyHint=hints["readOnlyHint"],
            destructiveHint=hints["destructiveHint"],
            idempotentHint=hints["idempotentHint"],
            openWorldHint=hints["openWorldHint"],
        )

    @server.tool(
        name="context",
        description=_description("context"),
        annotations=annotations("context"),
        structured_output=True,
    )
    def context(
        budget_bytes: int | None = None,
        budget_tokens: int | None = None,
    ) -> _SdkOperationEnvelope:
        arguments = _present(budget_bytes=budget_bytes, budget_tokens=budget_tokens)
        return sdk_result(application.call_tool("context", arguments))  # type: ignore[return-value]

    @server.tool(
        name="query",
        description=_description("query"),
        annotations=annotations("query"),
        structured_output=True,
    )
    def query(
        kinds: list[str] | None = None,
        ids: list[str] | None = None,
        title_contains: str | None = None,
        predicates: list[str] | None = None,
        source_ids: list[str] | None = None,
        source_revision_ids: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        summary_only: bool = False,
    ) -> _SdkOperationEnvelope:
        arguments = _present(
            kinds=kinds,
            ids=ids,
            title_contains=title_contains,
            predicates=predicates,
            source_ids=source_ids,
            source_revision_ids=source_revision_ids,
            statuses=statuses,
            limit=limit,
            offset=offset,
        )
        if summary_only:
            arguments["summary_only"] = True
        return sdk_result(application.call_tool("query", arguments))  # type: ignore[return-value]

    @server.tool(
        name="proposal_validate",
        description=_description("proposal_validate"),
        annotations=annotations("proposal_validate"),
        structured_output=True,
    )
    def proposal_validate(proposal: dict[str, Any]) -> _SdkOperationEnvelope:
        return sdk_result(
            application.call_tool("proposal_validate", {"proposal": proposal})
        )  # type: ignore[return-value]

    @server.tool(
        name="proposal_commit",
        description=_description("proposal_commit"),
        annotations=annotations("proposal_commit"),
        structured_output=True,
    )
    def proposal_commit(proposal: dict[str, Any]) -> _SdkOperationEnvelope:
        return sdk_result(
            application.call_tool("proposal_commit", {"proposal": proposal})
        )  # type: ignore[return-value]

    @server.tool(
        name="source_add",
        description=_description("source_add"),
        annotations=annotations("source_add"),
        structured_output=True,
    )
    def source_add(
        path: str, source_id: str | None = None
    ) -> _SdkOperationEnvelope:
        return sdk_result(
            application.call_tool(
                "source_add", _present(path=path, source_id=source_id)
            )
        )  # type: ignore[return-value]

    @server.tool(
        name="conflict_list",
        description=_description("conflict_list"),
        annotations=annotations("conflict_list"),
        structured_output=True,
    )
    def conflict_list(status: str | None = None) -> _SdkOperationEnvelope:
        return sdk_result(
            application.call_tool("conflict_list", _present(status=status))
        )  # type: ignore[return-value]

    @server.tool(
        name="ledger_verify",
        description=_description("ledger_verify"),
        annotations=annotations("ledger_verify"),
        structured_output=True,
    )
    def ledger_verify() -> _SdkOperationEnvelope:
        return sdk_result(application.call_tool("ledger_verify", {}))  # type: ignore[return-value]

    _register_resources(server, application)
    return server


def _register_resources(server: Any, application: McpApplication) -> None:
    def register(uri: str, function: Any) -> None:
        descriptor = next(item for item in _RESOURCE_DEFINITIONS if item["uri"] == uri)
        server.resource(
            uri,
            name=descriptor["name"],
            description=descriptor["description"],
            mime_type=descriptor["mimeType"],
        )(function)

    def workspace_info() -> str:
        return _resource_content(application, "shared-mind://workspace/info")

    def workspace_context() -> str:
        return _resource_content(application, "shared-mind://workspace/context")

    def projection_json() -> str:
        return _resource_content(
            application, "shared-mind://projection/project.json"
        )

    def projection_markdown() -> str:
        return _resource_content(application, "shared-mind://projection/project.md")

    def contract_schema() -> str:
        return _resource_content(application, "shared-mind://contract/schema")

    def predicate_registry() -> str:
        return _resource_content(
            application, "shared-mind://contract/predicate-registry"
        )

    for uri, function in zip(
        RESOURCE_URIS,
        (
            workspace_info,
            workspace_context,
            projection_json,
            projection_markdown,
            contract_schema,
            predicate_registry,
        ),
    ):
        register(uri, function)


def _resource_content(application: McpApplication, uri: str) -> str:
    return application.read_resource(uri)["contents"][0]["text"]


def _description(name: str) -> str:
    return next(
        item["description"] for item in _TOOL_DEFINITIONS if item["name"] == name
    )


def _present(**values: Any) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


def _argument_error(message: str) -> OperationResult:
    return OperationResult(
        False,
        "MCP_ARGUMENT_INVALID",
        message=message,
        exit_code=3,
    )


def _tool_result(result: OperationResult) -> dict[str, Any]:
    envelope = result.as_dict()
    return {
        "content": [{"type": "text", "text": canonical_json(envelope)}],
        "structuredContent": envelope,
        "isError": not result.ok,
    }


def _receipt_data(receipt: Any) -> dict[str, Any]:
    data = {
        "proposal_id": receipt.proposal_id,
        "ledger_sequence": receipt.ledger_seq,
        "state_root": receipt.state_root,
        "reason_codes": list(receipt.reason_codes),
        "conflict_ids": list(receipt.conflict_ids),
    }
    if isinstance(getattr(receipt, "document", None), dict):
        data["decision_receipt"] = receipt.document
    return data


def _receipt_result(receipt: Any, data: dict[str, Any]) -> OperationResult:
    outcome = str(receipt.outcome)
    ok = outcome in ("COMMITTED", "FACT_CONFLICT")
    exit_code = {
        "COMMITTED": 0,
        "FACT_CONFLICT": 0,
        "TRANSACTION_CONFLICT": 4,
        "VALIDATION_ERROR": 3,
    }.get(outcome, 70)
    return OperationResult(
        ok,
        outcome,
        data=data,
        message=None if ok else "Proposal was not committed.",
        exit_code=exit_code,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shared-mind-mcp")
    parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="Workspace root or a path inside it.",
    )
    arguments = parser.parse_args(argv)
    workspace = Workspace.open(arguments.workspace)
    server = create_server(workspace)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a console transport
    raise SystemExit(main())


__all__ = [
    "McpApplication",
    "RESOURCE_URIS",
    "TOOL_NAMES",
    "create_server",
    "main",
]
