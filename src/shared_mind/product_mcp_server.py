"""Optional MCP surface for Shared Mind product workflows.

This module is intentionally separate from :mod:`shared_mind.mcp_server` so the
kernel adapter keeps its frozen tool/resource allowlist.  All tools are bound
to one resolved workspace, paths are confined to that workspace, and canonical
state changes still flow through DraftProposal -> Proposal commit.
"""

from __future__ import annotations

import argparse
import copy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .product import ProductError, ProductService
from .workspace import Workspace, WorkspaceError


TOOL_NAMES = (
    "ingest",
    "extract",
    "draft_list",
    "draft_show",
    "draft_edit",
    "draft_reject",
    "draft_commit",
    "product_build",
    "task_context",
    "search",
    "memory_tool",
    "skill_mark_tested",
    "skill_approve",
    "cold_start",
    "continuity_evaluate",
    "product_verify",
)

RESOURCE_URIS = (
    "shared-mind-product://workspace/info",
    "shared-mind-product://catalog",
    "shared-mind-product://review-queue",
    "shared-mind-product://capabilities",
)


def _object_schema(
    properties: Mapping[str, Any] | None = None, *, required: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }


_STRINGS = {"type": "array", "items": {"type": "string", "minLength": 1}}
_TOOL_DEFINITIONS = (
    {
        "name": "ingest",
        "description": "Register workspace-local files/directories as immutable source revisions.",
        "inputSchema": _object_schema(
            {
                "paths": _STRINGS,
                "conversation_paths": _STRINGS,
                "include_code": {"type": "boolean"},
            },
            required=("paths",),
        ),
        "readOnly": False,
    },
    {
        "name": "extract",
        "description": "Create reviewable deterministic DraftProposals for one ingest batch.",
        "inputSchema": _object_schema(
            {"batch_id": {"type": "string", "minLength": 1}}, required=("batch_id",)
        ),
        "readOnly": False,
    },
    {
        "name": "draft_list",
        "description": "List staged DraftProposals.",
        "inputSchema": _object_schema(
            {
                "status": {"type": "string"},
                "draft_kind": {"type": "string"},
                "batch_id": {"type": "string"},
            }
        ),
        "readOnly": True,
    },
    {
        "name": "draft_show",
        "description": "Read one staged DraftProposal.",
        "inputSchema": _object_schema(
            {"draft_id": {"type": "string", "minLength": 1}}, required=("draft_id",)
        ),
        "readOnly": True,
    },
    {
        "name": "draft_edit",
        "description": "Replace a reviewable draft document with optimistic version checking.",
        "inputSchema": _object_schema(
            {
                "draft_id": {"type": "string", "minLength": 1},
                "document": {"type": "object"},
                "expected_version": {"type": "integer", "minimum": 1},
            },
            required=("draft_id", "document", "expected_version"),
        ),
        "readOnly": False,
    },
    {
        "name": "draft_reject",
        "description": "Reject a staged DraftProposal while retaining audit provenance.",
        "inputSchema": _object_schema(
            {
                "draft_id": {"type": "string", "minLength": 1},
                "rationale": {"type": "string", "minLength": 1},
            },
            required=("draft_id", "rationale"),
        ),
        "readOnly": False,
    },
    {
        "name": "draft_commit",
        "description": "Commit one reviewed draft through the canonical Proposal boundary.",
        "inputSchema": _object_schema(
            {"draft_id": {"type": "string", "minLength": 1}}, required=("draft_id",)
        ),
        "readOnly": False,
    },
    {
        "name": "product_build",
        "description": "Rebuild disposable Scenario/Core/retrieval/code views.",
        "inputSchema": _object_schema(
            {"target": {"type": "string", "enum": ["views", "indexes", "all"]}}
        ),
        "readOnly": False,
    },
    {
        "name": "task_context",
        "description": "Build deterministic context from one Shared State for the current task.",
        "inputSchema": _object_schema(
            {
                "task": {"type": "string", "minLength": 1},
                "purpose": {"type": "string"},
                "query": {"type": "string"},
                "references": _STRINGS,
                "depth": {"type": "string", "enum": ["SUMMARY", "DETAIL", "EVIDENCE"]},
                "budget_bytes": {"type": "integer", "minimum": 1},
                "budget_tokens": {"type": "integer", "minimum": 1},
            },
            required=("task",),
        ),
        "readOnly": True,
    },
    {
        "name": "search",
        "description": "Search canonical and derived memory with local lexical retrieval.",
        "inputSchema": _object_schema(
            {
                "query": {"type": "string", "minLength": 1},
                "kinds": _STRINGS,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            required=("query",),
        ),
        "readOnly": True,
    },
    {
        "name": "memory_tool",
        "description": (
            "Call one on-demand shared-memory capability such as source-span, "
            "Scenario, Skill, symbol, impact, or link-graph lookup."
        ),
        "inputSchema": _object_schema(
            {
                "name": {
                    "type": "string",
                    "enum": [
                        "capabilities",
                        "search",
                        "read_source_span",
                        "get_artifact",
                        "get_skill",
                        "find_symbol",
                        "get_symbol",
                        "impact_path",
                        "link_graph",
                    ],
                },
                "arguments": {"type": "object"},
            },
            required=("name",),
        ),
        "readOnly": True,
    },
    {
        "name": "skill_mark_tested",
        "description": "Record explicit passing validation evidence for a shared Skill version.",
        "inputSchema": _object_schema(
            {
                "skill_id": {"type": "string", "minLength": 1},
                "version": {"type": "integer", "minimum": 1},
                "evidence": {"type": "object"},
            },
            required=("skill_id", "version", "evidence"),
        ),
        "readOnly": False,
    },
    {
        "name": "skill_approve",
        "description": "Approve a TESTED shared Skill version with reviewer provenance.",
        "inputSchema": _object_schema(
            {
                "skill_id": {"type": "string", "minLength": 1},
                "version": {"type": "integer", "minimum": 1},
                "approval": {"type": "object"},
            },
            required=("skill_id", "version", "approval"),
        ),
        "readOnly": False,
    },
    {
        "name": "cold_start",
        "description": "Run workspace-local bulk ingest through the first deterministic handoff.",
        "inputSchema": _object_schema(
            {
                "paths": _STRINGS,
                "conversation_paths": _STRINGS,
                "auto_commit_deterministic": {"type": "boolean"},
                "task": {"type": "string", "minLength": 1},
                "budget_bytes": {"type": "integer", "minimum": 1},
            },
            required=("paths",),
        ),
        "readOnly": False,
    },
    {
        "name": "continuity_evaluate",
        "description": (
            "Deterministically score a fresh-session observation against one "
            "Shared State context without mutating canonical memory."
        ),
        "inputSchema": _object_schema(
            {
                "evaluation": {
                    "type": "string",
                    "enum": [
                        "ZERO_RELEARNING",
                        "MEMORY_POLLUTION",
                        "MEMORY_LIFECYCLE",
                        "CONFLICT_RESOLUTION",
                        "CONTEXT_QUALITY",
                        "PAIRED_CONTEXT_REDUCTION"
                    ],
                },
                "context": {"type": "object"},
                "observation": {"type": "object"},
                "expectation": {"type": "object"},
                "baseline_context": {"type": "object"},
                "baseline_observation": {"type": "object"},
                "candidate_context": {"type": "object"},
                "candidate_observation": {"type": "object"},
                "thresholds": {"type": "object"},
                "memories": {"type": "array", "items": {"type": "object"}},
                "expected_truth": {"type": "object"},
                "confident_threshold": {"type": "number", "minimum": 0, "maximum": 1},
                "before": {"type": "object"},
                "after": {"type": "object"},
                "elapsed_ms": {"type": "number", "minimum": 0},
                "token_count": {"type": "integer", "minimum": 0},
                "baseline_elapsed_ms": {"type": "number", "minimum": 0},
                "candidate_elapsed_ms": {"type": "number", "minimum": 0},
                "baseline_token_count": {"type": "integer", "minimum": 0},
                "candidate_token_count": {"type": "integer", "minimum": 0},
            },
            required=("evaluation",),
        ),
        "readOnly": True,
    },
    {
        "name": "product_verify",
        "description": "Verify the canonical ledger and product audit/derived provenance.",
        "inputSchema": _object_schema(),
        "readOnly": True,
    },
)

_RESOURCE_DEFINITIONS = (
    {
        "uri": RESOURCE_URIS[0],
        "name": "Product workspace information",
        "description": "Resolved workspace and product-store metadata.",
        "mimeType": "application/json",
    },
    {
        "uri": RESOURCE_URIS[1],
        "name": "Shared state catalog",
        "description": "Canonical records, derived views, Skills, and staged drafts.",
        "mimeType": "application/json",
    },
    {
        "uri": RESOURCE_URIS[2],
        "name": "Review queue",
        "description": "Draft, conflict, stale view, and Skill promotion work.",
        "mimeType": "application/json",
    },
    {
        "uri": RESOURCE_URIS[3],
        "name": "On-demand capabilities",
        "description": "Search, source-span, Scenario, Skill, symbol, and impact capabilities.",
        "mimeType": "application/json",
    },
)


class ProductMcpApplication:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.service = ProductService(workspace)

    def close(self) -> None:
        self.service.close()

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item["name"],
                "description": item["description"],
                "inputSchema": copy.deepcopy(item["inputSchema"]),
                "annotations": {
                    "readOnlyHint": item["readOnly"],
                    "destructiveHint": not item["readOnly"],
                    "idempotentHint": item["name"] not in {"draft_commit", "cold_start"},
                    "openWorldHint": False,
                },
            }
            for item in _TOOL_DEFINITIONS
        ]

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        definition = next((item for item in _TOOL_DEFINITIONS if item["name"] == name), None)
        if definition is None:
            return _result(False, "MCP_TOOL_NOT_FOUND", message=f"Unknown product tool: {name}")
        if arguments is None:
            values: dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            values = dict(arguments)
        else:
            return _result(False, "MCP_ARGUMENT_INVALID", message="Arguments must be an object.")
        schema = definition["inputSchema"]
        unknown = sorted(set(values) - set(schema["properties"]))
        missing = sorted(set(schema["required"]) - set(values))
        if unknown or missing:
            parts = []
            if unknown:
                parts.append("unknown=" + ",".join(unknown))
            if missing:
                parts.append("missing=" + ",".join(missing))
            return _result(False, "MCP_ARGUMENT_INVALID", message="; ".join(parts))
        try:
            data, code = self._dispatch(name, values)
            return _result(True, code, data=data)
        except ProductError as exc:
            return _result(False, exc.code, data=exc.data, message=exc.message)
        except (WorkspaceError, TypeError, ValueError, KeyError) as exc:
            return _result(
                False,
                getattr(exc, "code", "MCP_ARGUMENT_INVALID"),
                message=getattr(exc, "message", str(exc)),
            )

    def list_resources(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(_RESOURCE_DEFINITIONS))

    def read_resource(self, uri: str) -> dict[str, list[dict[str, str]]]:
        descriptor = next((item for item in _RESOURCE_DEFINITIONS if item["uri"] == uri), None)
        if descriptor is None:
            raise ValueError(f"Unknown product resource URI: {uri}")
        value: Any
        if uri == RESOURCE_URIS[0]:
            value = self.service.describe()
        elif uri == RESOURCE_URIS[1]:
            value = self.service.catalog()
        elif uri == RESOURCE_URIS[2]:
            value = self.service.review_queue()
        else:
            value = self.service.retrieval.capabilities()
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": descriptor["mimeType"],
                    "text": canonical_json(value) + "\n",
                }
            ]
        }

    def _dispatch(self, name: str, values: dict[str, Any]) -> tuple[Any, str]:
        if name == "ingest":
            paths = self._safe_paths(values.get("paths", []))
            conversations = self._safe_paths(values.get("conversation_paths", []))
            return (
                self.service.ingest(
                    paths,
                    conversation_paths=conversations,
                    include_code=bool(values.get("include_code", True)),
                ),
                "INGEST_COMPLETED",
            )
        if name == "extract":
            return self.service.extract(str(values["batch_id"])), "EXTRACTION_COMPLETED"
        if name == "draft_list":
            drafts = self.service.list_drafts(
                status=values.get("status"),
                draft_kind=values.get("draft_kind"),
                batch_id=values.get("batch_id"),
            )
            return {"drafts": drafts, "count": len(drafts)}, "DRAFTS_LISTED"
        if name == "draft_show":
            return self.service.get_draft(str(values["draft_id"])), "DRAFT_SHOWN"
        if name == "draft_edit":
            document = values["document"]
            if not isinstance(document, Mapping):
                raise TypeError("document must be an object")
            return (
                self.service.edit_draft(
                    str(values["draft_id"]),
                    document,
                    expected_version=int(values["expected_version"]),
                ),
                "DRAFT_UPDATED",
            )
        if name == "draft_reject":
            return (
                self.service.reject_draft(
                    str(values["draft_id"]), rationale=str(values["rationale"])
                ),
                "DRAFT_REJECTED",
            )
        if name == "draft_commit":
            return self.service.commit_draft(str(values["draft_id"])), "DRAFT_COMMITTED"
        if name == "product_build":
            target = str(values.get("target", "all"))
            data: dict[str, Any] = {}
            if target in {"views", "all"}:
                data["views"] = self.service.build_memory_views()
            if target in {"indexes", "all"}:
                data["indexes"] = self.service.build_indexes()
            return data, "PRODUCT_VIEWS_BUILT"
        if name == "task_context":
            return (
                self.service.context(
                    {
                        "task": values["task"],
                        "purpose": values.get("purpose"),
                        "query": values.get("query"),
                        "references": values.get("references", []),
                        "depth": values.get("depth", "DETAIL"),
                        "budget_bytes": values.get("budget_bytes"),
                        "budget_tokens": values.get("budget_tokens"),
                        "hints": {},
                    }
                ),
                "TASK_CONTEXT_READY",
            )
        if name == "search":
            return (
                self.service.search(
                    str(values["query"]),
                    kinds=tuple(values.get("kinds", [])),
                    limit=int(values.get("limit", 20)),
                ),
                "SEARCH_COMPLETED",
            )
        if name == "memory_tool":
            arguments = values.get("arguments", {})
            if not isinstance(arguments, Mapping):
                raise TypeError("arguments must be an object")
            return (
                self.service.tool_call(str(values["name"]), arguments),
                "MEMORY_TOOL_COMPLETED",
            )
        if name == "skill_mark_tested":
            evidence = values["evidence"]
            if not isinstance(evidence, Mapping):
                raise TypeError("evidence must be an object")
            return (
                self.service.record_skill_test(
                    str(values["skill_id"]),
                    int(values["version"]),
                    evidence=evidence,
                ),
                "SKILL_TEST_RECORDED",
            )
        if name == "skill_approve":
            approval = values["approval"]
            if not isinstance(approval, Mapping):
                raise TypeError("approval must be an object")
            return (
                self.service.approve_skill(
                    str(values["skill_id"]),
                    int(values["version"]),
                    approval=approval,
                ),
                "SKILL_APPROVED",
            )
        if name == "cold_start":
            return (
                self.service.cold_start(
                    self._safe_paths(values.get("paths", [])),
                    conversation_paths=self._safe_paths(values.get("conversation_paths", [])),
                    auto_commit_deterministic=bool(
                        values.get("auto_commit_deterministic", True)
                    ),
                    task=str(
                        values.get("task", "Continue the highest-priority project work.")
                    ),
                    budget_bytes=int(values.get("budget_bytes", 64 * 1024)),
                ),
                "COLD_START_COMPLETED",
            )
        if name == "continuity_evaluate":
            evaluation = values["evaluation"]
            if evaluation in {"ZERO_RELEARNING", "CONTEXT_QUALITY"}:
                context = values["context"]
                observation = values["observation"]
                expectation = values["expectation"]
                if not all(
                    isinstance(item, Mapping)
                    for item in (context, observation, expectation)
                ):
                    raise TypeError(
                        "context, observation, and expectation must be objects"
                    )
            if evaluation == "ZERO_RELEARNING":
                return (
                    self.service.evaluate_zero_relearning(
                        context,
                        observation,
                        expectation,
                        elapsed_ms=values["elapsed_ms"],
                        token_count=values["token_count"],
                    ),
                    "ZERO_RELEARNING_EVALUATED",
                )
            if evaluation == "MEMORY_POLLUTION":
                memories = values["memories"]
                expected_truth = values["expected_truth"]
                if (
                    isinstance(memories, (str, bytes))
                    or not isinstance(memories, Sequence)
                    or not isinstance(expected_truth, Mapping)
                ):
                    raise TypeError("memories must be an array and expected_truth an object")
                return (
                    self.service.evaluate_memory_pollution(
                        memories,
                        expected_truth=expected_truth,
                        confident_threshold=float(values.get("confident_threshold", 0.9)),
                    ),
                    "MEMORY_POLLUTION_EVALUATED",
                )
            if evaluation == "MEMORY_LIFECYCLE":
                return self.service.memory_lifecycle_inventory(), "MEMORY_LIFECYCLE_READY"
            if evaluation == "CONFLICT_RESOLUTION":
                before = values["before"]
                after = values["after"]
                if not isinstance(before, Mapping) or not isinstance(after, Mapping):
                    raise TypeError("before and after must be objects")
                return (
                    self.service.evaluate_conflict_resolution(before, after),
                    "CONFLICT_RESOLUTION_EVALUATED",
                )
            if evaluation == "PAIRED_CONTEXT_REDUCTION":
                paired = (
                    values.get("baseline_context"),
                    values.get("baseline_observation"),
                    values.get("candidate_context"),
                    values.get("candidate_observation"),
                    values.get("expectation"),
                    values.get("thresholds"),
                )
                if not all(isinstance(item, Mapping) for item in paired):
                    raise TypeError(
                        "paired contexts, observations, expectation, and thresholds "
                        "must be objects"
                    )
                return (
                    self.service.evaluate_paired_context_reduction(
                        *paired,
                        baseline_elapsed_ms=values["baseline_elapsed_ms"],
                        candidate_elapsed_ms=values["candidate_elapsed_ms"],
                        baseline_token_count=values["baseline_token_count"],
                        candidate_token_count=values["candidate_token_count"],
                    ),
                    "CONTEXT_REDUCTION_EVALUATED",
                )
            return (
                self.service.evaluate_context_quality(
                    context,
                    observation,
                    expectation,
                    elapsed_ms=values["elapsed_ms"],
                    token_count=values["token_count"],
                ),
                "CONTEXT_QUALITY_EVALUATED",
            )
        report = self.service.verify()
        return report, "PRODUCT_INTEGRITY_VALID" if report["valid"] else "PRODUCT_INTEGRITY_INVALID"

    def _safe_paths(self, values: Sequence[Any]) -> list[Path]:
        paths: list[Path] = []
        root = self.workspace.root.resolve()
        for value in values:
            path = Path(str(value))
            if path.is_absolute():
                raise WorkspaceError(
                    "PATH_OUTSIDE_WORKSPACE",
                    "Product MCP paths must be relative to the fixed workspace.",
                )
            resolved = (root / path).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise WorkspaceError(
                    "PATH_OUTSIDE_WORKSPACE", f"Path escapes the workspace: {value}"
                ) from exc
            paths.append(resolved)
        return paths


def _result(
    ok: bool,
    code: str,
    *,
    data: Any | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {"ok": ok, "code": code}
    if data is not None:
        envelope["data"] = data
    if message is not None:
        envelope["message"] = message
    return {
        "content": [{"type": "text", "text": canonical_json(envelope)}],
        "structuredContent": envelope,
        "isError": not ok,
    }


def create_server(workspace: Workspace) -> Any:
    from mcp.types import ToolAnnotations

    try:
        from mcp.server import MCPServer

        server_type = MCPServer
        sdk_v2 = True
    except ImportError:  # pragma: no cover - MCP v1 compatibility
        from mcp.server.fastmcp import FastMCP

        server_type = FastMCP
        sdk_v2 = False

    application = ProductMcpApplication(workspace)
    server = server_type(
        "shared-mind-product",
        instructions=(
            "One Shared State product adapter. Use staging/review for extracted memory; "
            "never create Agent-specific memory partitions."
        ),
    )

    def annotations(read_only: bool) -> Any:
        values = {
            "read_only_hint": read_only,
            "destructive_hint": not read_only,
            "idempotent_hint": read_only,
            "open_world_hint": False,
        }
        if sdk_v2:
            return ToolAnnotations(**values)
        return ToolAnnotations(
            readOnlyHint=values["read_only_hint"],
            destructiveHint=values["destructive_hint"],
            idempotentHint=values["idempotent_hint"],
            openWorldHint=False,
        )

    def register(name: str, function: Any) -> None:
        definition = next(item for item in _TOOL_DEFINITIONS if item["name"] == name)
        server.tool(
            name=name,
            description=definition["description"],
            annotations=annotations(definition["readOnly"]),
            structured_output=True,
        )(function)

    def invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = application.call_tool(name, arguments)
        if result["isError"]:
            raise RuntimeError(canonical_json(result["structuredContent"]))
        return result["structuredContent"]

    def ingest(
        paths: list[str], conversation_paths: list[str] | None = None, include_code: bool = True
    ) -> dict[str, Any]:
        return invoke(
            "ingest",
            {
                "paths": paths,
                "conversation_paths": conversation_paths or [],
                "include_code": include_code,
            },
        )

    def extract(batch_id: str) -> dict[str, Any]:
        return invoke("extract", {"batch_id": batch_id})

    def draft_list(
        status: str | None = None,
        draft_kind: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        return invoke(
            "draft_list",
            {key: value for key, value in locals().items() if value is not None},
        )

    def draft_show(draft_id: str) -> dict[str, Any]:
        return invoke("draft_show", {"draft_id": draft_id})

    def draft_edit(
        draft_id: str, document: dict[str, Any], expected_version: int
    ) -> dict[str, Any]:
        return invoke(
            "draft_edit",
            {
                "draft_id": draft_id,
                "document": document,
                "expected_version": expected_version,
            },
        )

    def draft_reject(draft_id: str, rationale: str) -> dict[str, Any]:
        return invoke("draft_reject", {"draft_id": draft_id, "rationale": rationale})

    def draft_commit(draft_id: str) -> dict[str, Any]:
        return invoke("draft_commit", {"draft_id": draft_id})

    def product_build(target: str = "all") -> dict[str, Any]:
        return invoke("product_build", {"target": target})

    def task_context(
        task: str,
        purpose: str | None = None,
        query: str | None = None,
        references: list[str] | None = None,
        depth: str = "DETAIL",
        budget_bytes: int | None = None,
        budget_tokens: int | None = None,
    ) -> dict[str, Any]:
        values = {
            "task": task,
            "purpose": purpose,
            "query": query,
            "references": references,
            "depth": depth,
            "budget_bytes": budget_bytes,
            "budget_tokens": budget_tokens,
        }
        return invoke("task_context", {key: value for key, value in values.items() if value is not None})

    def search(query: str, kinds: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        return invoke("search", {"query": query, "kinds": kinds or [], "limit": limit})

    def cold_start(
        paths: list[str],
        conversation_paths: list[str] | None = None,
        auto_commit_deterministic: bool = True,
        task: str = "Continue the highest-priority project work.",
        budget_bytes: int = 64 * 1024,
    ) -> dict[str, Any]:
        return invoke(
            "cold_start",
            {
                "paths": paths,
                "conversation_paths": conversation_paths or [],
                "auto_commit_deterministic": auto_commit_deterministic,
                "task": task,
                "budget_bytes": budget_bytes,
            },
        )

    def product_verify() -> dict[str, Any]:
        return invoke("product_verify", {})

    for name, function in (
        ("ingest", ingest),
        ("extract", extract),
        ("draft_list", draft_list),
        ("draft_show", draft_show),
        ("draft_edit", draft_edit),
        ("draft_reject", draft_reject),
        ("draft_commit", draft_commit),
        ("product_build", product_build),
        ("task_context", task_context),
        ("search", search),
        ("cold_start", cold_start),
        ("product_verify", product_verify),
    ):
        register(name, function)

    for descriptor in _RESOURCE_DEFINITIONS:
        uri = descriptor["uri"]
        server.resource(
            uri,
            name=descriptor["name"],
            description=descriptor["description"],
            mime_type=descriptor["mimeType"],
        )(_resource_reader(application, uri))
    return server


def _resource_reader(application: ProductMcpApplication, uri: str) -> Any:
    """Bind one resource URI without declaring it as a handler parameter.

    The SDK derives URI template variables from the handler signature, so a
    default-argument capture (``def resource(uri: str = uri)``) registers as a
    template variable and rejects these static URIs at startup.
    """

    def resource() -> str:
        return application.read_resource(uri)["contents"][0]["text"]

    return resource


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shared-mind-product-mcp")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    args = parser.parse_args(argv)
    workspace = Workspace.open(args.workspace)
    server = create_server(workspace)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ProductMcpApplication",
    "RESOURCE_URIS",
    "TOOL_NAMES",
    "create_server",
    "main",
]
