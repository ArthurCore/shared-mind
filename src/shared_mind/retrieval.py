"""Local retrieval, link graph, and rebuildable code understanding."""

from __future__ import annotations

import ast
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .canonical import canonical_json, sha256_bytes, sha256_json
from .memory_views import MemoryViewBuilder
from .product_store import ProductStore
from .workspace import Workspace


RETRIEVAL_INDEX_VERSION = "retrieval-index@2"
CODE_INDEX_VERSION = "python-code-index@1"


class RetrievalError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class VectorRanker(Protocol):
    ranker_id: str
    ranker_version: str

    def rank(
        self,
        query: str,
        documents: Sequence[Mapping[str, Any]],
        *,
        limit: int,
    ) -> Sequence[str]: ...


@dataclass(frozen=True)
class RetrievalBuildReport:
    documents: int
    links: int
    symbols: int
    code_edges: int
    fts_enabled: bool
    fingerprint: str


class RetrievalService:
    def __init__(self, workspace: Workspace, store: ProductStore):
        self.workspace = workspace
        self.store = store
        self.views = MemoryViewBuilder(workspace, store)

    def rebuild(self) -> RetrievalBuildReport:
        projection = self.views.projection()
        atomic = self.views.atomic_records(projection)
        documents = self._documents(projection, atomic)
        links = self._links(atomic)
        symbols, edges = self._code_index(projection)
        source_updated_at = {
            item["source_revision"]["revision_id"]: item["source_revision"]["captured_at"]
            for item in projection["sources"]
        }
        symbol_documents = [
            {
                "document_id": symbol["symbol_id"],
                "kind": "CODE_SYMBOL",
                "title": symbol["qualified_name"],
                "body": canonical_json(symbol),
                "fingerprint": sha256_json(symbol),
                "metadata": {
                    "source_revision_id": symbol["source_revision_id"],
                    "file_path": symbol["file_path"],
                    "projection_ref": f"code://{symbol['symbol_id']}",
                },
                "updated_at": source_updated_at.get(
                    symbol["source_revision_id"], "1970-01-01T00:00:00Z"
                ),
            }
            for symbol in symbols
        ]
        documents.extend(symbol_documents)
        self.store.replace_retrieval_documents(documents)
        self.store.replace_links(links)
        self.store.replace_code_index(symbols, edges)
        fingerprint = sha256_json(
            {
                "version": RETRIEVAL_INDEX_VERSION,
                "state_root": projection["state_root"],
                "documents": [item["fingerprint"] for item in documents],
                "links": links,
                "symbols": [item["symbol_id"] for item in symbols],
                "edges": edges,
            }
        )
        return RetrievalBuildReport(
            documents=len(documents),
            links=len(links),
            symbols=len(symbols),
            code_edges=len(edges),
            fts_enabled=self.store.fts_enabled,
            fingerprint=fingerprint,
        )

    def search(
        self,
        query: str,
        *,
        kinds: Sequence[str] = (),
        limit: int = 20,
        vector_ranker: VectorRanker | None = None,
    ) -> dict[str, Any]:
        lexical = self.store.search(query, kinds=kinds, limit=max(limit, 50))
        if vector_ranker is None:
            return {
                "query": query,
                "mode": "LEXICAL",
                "results": lexical[:limit],
                "ranker": "sqlite-fts5-bm25" if self.store.fts_enabled else "deterministic-token-count",
                "retrieval_version": RETRIEVAL_INDEX_VERSION,
            }
        all_documents = [
            {
                "document_id": row["document_id"],
                "kind": row["kind"],
                "title": row["title"],
                "body": row["body"],
                "metadata": json.loads(row["metadata"]),
            }
            for row in self.store.connection.execute(
                "SELECT * FROM retrieval_documents ORDER BY document_id"
            )
            if not kinds or row["kind"] in kinds
        ]
        vector_ids = list(vector_ranker.rank(query, all_documents, limit=max(limit, 50)))
        lexical_ids = [item["document_id"] for item in lexical]
        combined = _rrf(lexical_ids, vector_ids)
        by_id = {item["document_id"]: item for item in lexical}
        for document in all_documents:
            by_id.setdefault(
                document["document_id"],
                document
                | {
                    "fingerprint": sha256_json(document),
                    "score": 0.0,
                },
            )
        results = [
            by_id[document_id] | {"rrf_score": score}
            for document_id, score in combined[:limit]
            if document_id in by_id
        ]
        return {
            "query": query,
            "mode": "HYBRID_RRF",
            "results": results,
            "retrieval_version": RETRIEVAL_INDEX_VERSION,
            "ranker": {
                "lexical": "sqlite-fts5-bm25" if self.store.fts_enabled else "deterministic-token-count",
                "vector": f"{vector_ranker.ranker_id}@{vector_ranker.ranker_version}",
                "fusion": "rrf@60",
            },
        }

    def capabilities(self) -> dict[str, Any]:
        tools = [
                "search",
                "read_source_span",
                "get_artifact",
                "get_skill",
                "find_symbol",
                "get_symbol",
                "impact_path",
                "link_graph",
            ]
        return {
            "protocol_version": "on-demand-tools@1",
            "capabilities": tools,
            "tools": tools,
            "fts_enabled": self.store.fts_enabled,
        }

    def read_source_span(
        self,
        revision_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> dict[str, Any]:
        if start_byte < 0 or end_byte is not None and end_byte <= start_byte:
            raise RetrievalError("SOURCE_SPAN_INVALID", "Invalid source byte range.")
        kernel = self.workspace.open_kernel()
        try:
            row = kernel.connection.execute(
                "SELECT document, content FROM sources WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if row is None:
                raise RetrievalError("SOURCE_REVISION_NOT_FOUND", revision_id)
            content = bytes(row["content"])
            effective_end = len(content) if end_byte is None else end_byte
            if effective_end > len(content):
                raise RetrievalError("SOURCE_SPAN_OUT_OF_RANGE", "Byte range exceeds source length.")
            excerpt = content[start_byte:effective_end]
            return {
                "source_revision": json.loads(row["document"]),
                "start_byte": start_byte,
                "end_byte": effective_end,
                "excerpt": excerpt.decode("utf-8"),
                "excerpt_hash": sha256_bytes(excerpt),
            }
        finally:
            kernel.close()

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.store.get_artifact(artifact_id)
        if artifact is None:
            raise RetrievalError("ARTIFACT_NOT_FOUND", artifact_id)
        return artifact

    def get_skill(self, skill_id: str, *, version: int | None = None) -> dict[str, Any]:
        skill = self.store.get_skill(skill_id, version=version, approved_only=version is None)
        if skill is None:
            raise RetrievalError("SKILL_NOT_FOUND", skill_id)
        return skill

    def get_symbol(self, symbol_id: str) -> dict[str, Any]:
        symbol = self.store.get_symbol(symbol_id)
        if symbol is None:
            raise RetrievalError("SYMBOL_NOT_FOUND", symbol_id)
        return symbol | {"edges": self.store.code_edges(symbol_id)}

    def impact_path(
        self,
        symbol_id: str,
        *,
        max_depth: int = 4,
        direction: str = "BOTH",
    ) -> dict[str, Any]:
        if self.store.get_symbol(symbol_id) is None:
            raise RetrievalError("SYMBOL_NOT_FOUND", symbol_id)
        direction = {
            "INCOMING": "REVERSE",
            "OUTGOING": "FORWARD",
        }.get(direction.upper(), direction.upper())
        if direction not in {"FORWARD", "REVERSE", "BOTH"}:
            raise RetrievalError(
                "IMPACT_DIRECTION_INVALID",
                "direction must be FORWARD, REVERSE, BOTH, INCOMING, or OUTGOING",
            )
        if max_depth < 1 or max_depth > 16:
            raise RetrievalError("IMPACT_DEPTH_INVALID", "max_depth must be 1..16")
        adjacency: dict[str, set[str]] = defaultdict(set)
        for row in self.store.connection.execute(
            "SELECT source_symbol_id, target_symbol_id FROM code_edges ORDER BY source_symbol_id, target_symbol_id"
        ):
            source = row["source_symbol_id"]
            target = row["target_symbol_id"]
            if direction in {"FORWARD", "BOTH"}:
                adjacency[source].add(target)
            if direction in {"REVERSE", "BOTH"}:
                adjacency[target].add(source)
        queue: deque[tuple[str, list[str]]] = deque([(symbol_id, [symbol_id])])
        visited = {symbol_id}
        paths: list[list[str]] = []
        while queue:
            current, path = queue.popleft()
            if len(path) - 1 >= max_depth:
                continue
            for neighbor in sorted(adjacency.get(current, set())):
                next_path = [*path, neighbor]
                paths.append(next_path)
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, next_path))
        relevant_edges = []
        for current in sorted(visited):
            for edge in self.store.code_edges(current):
                if (
                    edge["source_symbol_id"] in visited
                    and edge["target_symbol_id"] in visited
                    and edge not in relevant_edges
                ):
                    relevant_edges.append(edge)
        relevant_edges.sort(
            key=lambda item: (
                item["edge_kind"],
                item["source_symbol_id"],
                item["target_symbol_id"],
            )
        )
        return {
            "symbol_id": symbol_id,
            "direction": direction,
            "max_depth": max_depth,
            "impacted_symbol_ids": sorted(visited - {symbol_id}),
            "paths": paths,
            "edges": relevant_edges,
        }

    def evaluate(
        self,
        cases: Sequence[Mapping[str, Any]],
        *,
        limit: int = 10,
    ) -> dict[str, Any]:
        results = []
        recalls = []
        conflict_recalls = []
        latencies_ms: list[float] = []
        response_bytes: list[int] = []
        traceable = 0
        evidence_traceable = 0
        evidence_candidates = 0
        returned_total = 0
        for case in cases:
            started = time.perf_counter()
            response = self.search(str(case["query"]), limit=limit)
            latency_ms = (time.perf_counter() - started) * 1000
            latencies_ms.append(latency_ms)
            response_size = len(canonical_json(response).encode("utf-8"))
            response_bytes.append(response_size)
            returned_items = response["results"]
            returned = [item["document_id"] for item in returned_items]
            expected = set(case.get("expected_ids", []))
            recall = len(expected.intersection(returned)) / len(expected) if expected else 1.0
            recalls.append(recall)
            expected_conflicts = set(case.get("expected_conflict_ids", []))
            conflict_recall = (
                len(expected_conflicts.intersection(returned)) / len(expected_conflicts)
                if expected_conflicts
                else 1.0
            )
            conflict_recalls.append(conflict_recall)
            for item in returned_items:
                returned_total += 1
                metadata = item.get("metadata", {})
                if any(
                    key in metadata
                    for key in (
                        "projection_ref",
                        "source_revision_id",
                        "source_revision_ids",
                        "builder_version",
                        "skill_id",
                    )
                ):
                    traceable += 1
                if item.get("kind") in {"CLAIM", "EVIDENCE_LINK", "SOURCE_REVISION", "SOURCE_TEXT"}:
                    evidence_candidates += 1
                    if metadata.get("source_revision_id") or metadata.get(
                        "source_revision_ids"
                    ):
                        evidence_traceable += 1
            results.append(
                {
                    "case_id": case.get("case_id"),
                    "query": case["query"],
                    "expected_ids": sorted(expected),
                    "returned_ids": returned,
                    "recall": recall,
                    "conflict_recall": conflict_recall,
                    "latency_ms": latency_ms,
                    "response_bytes": response_size,
                }
            )
        ordered_latency = sorted(latencies_ms)
        p95_index = max(0, math.ceil(len(ordered_latency) * 0.95) - 1)
        return {
            "cases": len(results),
            "case_count": len(results),
            "mean_recall": sum(recalls) / len(recalls) if recalls else 1.0,
            "mean_conflict_recall": (
                sum(conflict_recalls) / len(conflict_recalls)
                if conflict_recalls
                else 1.0
            ),
            "traceability_rate": traceable / returned_total if returned_total else 1.0,
            "evidence_traceability_rate": (
                evidence_traceable / evidence_candidates
                if evidence_candidates
                else 1.0
            ),
            "mean_response_bytes": (
                sum(response_bytes) / len(response_bytes) if response_bytes else 0.0
            ),
            "p95_latency_ms": ordered_latency[p95_index] if ordered_latency else 0.0,
            "results": results,
        }

    def _documents(
        self,
        projection: Mapping[str, Any],
        atomic: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        for record in atomic:
            document = {
                "document_id": record["object_id"],
                "kind": record["kind"],
                "title": record["title"],
                "body": f"{record['summary']}\n{canonical_json(record['document'])}",
                "metadata": {
                    "status": record["status"],
                    "source_revision_ids": record["source_revision_ids"],
                    "related_ids": record["related_ids"],
                    "projection_ref": record["projection_ref"],
                },
                "updated_at": _record_updated_at(record, projection),
            }
            document["fingerprint"] = sha256_json(
                {key: value for key, value in document.items() if key != "updated_at"}
            )
            documents.append(document)
        kernel = self.workspace.open_kernel()
        try:
            for item in projection["sources"]:
                source = item["source_revision"]
                row = kernel.connection.execute(
                    "SELECT content FROM sources WHERE revision_id=?", (source["revision_id"],)
                ).fetchone()
                if row is None:
                    continue
                body = bytes(row["content"]).decode("utf-8")
                document = {
                    "document_id": f"source-text:{source['revision_id']}",
                    "kind": "SOURCE_TEXT",
                    "title": source["title"],
                    "body": body,
                    "metadata": {
                        "source_revision_id": source["revision_id"],
                        "source_id": source["source_id"],
                        "content_hash": source["content_hash"],
                        "projection_ref": item["projection_ref"],
                    },
                    "updated_at": source["captured_at"],
                }
                document["fingerprint"] = source["content_hash"]
                documents.append(document)
        finally:
            kernel.close()
        for artifact in self.store.list_artifacts(status="READY"):
            document = {
                "document_id": artifact["artifact_id"],
                "kind": artifact["artifact_type"],
                "title": artifact["title"],
                "body": canonical_json(artifact["document"]),
                "fingerprint": artifact["dependency_digest"],
                "metadata": {
                    "scope": artifact["scope"],
                    "version": artifact["version"],
                    "builder_version": artifact["builder_version"],
                },
                "updated_at": artifact["updated_at"],
            }
            documents.append(document)
        for skill in self.store.list_skills(status="APPROVED"):
            documents.append(
                {
                    "document_id": f"skill-version:{skill['skill_id']}@{skill['version']}",
                    "kind": "SKILL",
                    "title": skill["document"]["purpose"],
                    "body": canonical_json(skill["document"]),
                    "fingerprint": skill["content_hash"],
                    "metadata": {
                        "skill_id": skill["skill_id"],
                        "version": skill["version"],
                        "status": skill["status"],
                    },
                    "updated_at": skill["updated_at"],
                }
            )
        documents.sort(key=lambda item: item["document_id"])
        return documents

    def _links(self, atomic: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        for record in atomic:
            for target in record["related_ids"]:
                links.append(
                    {
                        "source_id": record["object_id"],
                        "target_id": target,
                        "relation": "RELATED_TO",
                        "metadata": {"source_kind": record["kind"]},
                    }
                )
            for revision_id in record["source_revision_ids"]:
                links.append(
                    {
                        "source_id": record["object_id"],
                        "target_id": revision_id,
                        "relation": "DERIVED_FROM",
                        "metadata": {"source_kind": record["kind"]},
                    }
                )
        for artifact in self.store.list_artifacts(status="READY"):
            for member_id in artifact["document"].get("member_object_ids", []):
                links.append(
                    {
                        "source_id": artifact["artifact_id"],
                        "target_id": member_id,
                        "relation": "CONTAINS",
                        "metadata": {"artifact_type": artifact["artifact_type"]},
                    }
                )
        for skill in self.store.list_skills():
            for resource in skill["document"].get("resources", []):
                revision_id = resource.get("source_revision_id")
                if revision_id:
                    links.append(
                        {
                            "source_id": f"skill-version:{skill['skill_id']}@{skill['version']}",
                            "target_id": revision_id,
                            "relation": "USES_RESOURCE",
                            "metadata": {"path": resource["path"]},
                        }
                    )
        deduped = {
            (link["source_id"], link["target_id"], link["relation"]): link
            for link in links
        }
        return [deduped[key] for key in sorted(deduped)]

    def _code_index(
        self, projection: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        kernel = self.workspace.open_kernel()
        try:
            sources = []
            for item in projection["sources"]:
                source = item["source_revision"]
                if source["media_type"] != "text/x-python" and not source["title"].endswith(".py"):
                    continue
                row = kernel.connection.execute(
                    "SELECT content FROM sources WHERE revision_id=?", (source["revision_id"],)
                ).fetchone()
                if row:
                    sources.append((source, bytes(row["content"]).decode("utf-8")))
        finally:
            kernel.close()
        symbols: list[dict[str, Any]] = []
        calls_by_symbol: dict[str, list[str]] = defaultdict(list)
        references_by_symbol: dict[str, list[str]] = defaultdict(list)
        by_name: dict[str, list[str]] = defaultdict(list)
        for source, text in sorted(sources, key=lambda item: item[0]["revision_id"]):
            try:
                tree = ast.parse(text, filename=source["title"])
            except SyntaxError:
                continue
            module_id = _symbol_id(source["revision_id"], source["title"], "<module>", 1)
            module_symbol = {
                "symbol_id": module_id,
                "source_revision_id": source["revision_id"],
                "file_path": _source_file_path(source),
                "name": "<module>",
                "qualified_name": source["title"],
                "symbol_kind": "MODULE",
                "start_line": 1,
                "end_line": max(1, len(text.splitlines())),
                "signature": None,
                "index_version": CODE_INDEX_VERSION,
            }
            symbols.append(module_symbol)
            by_name["<module>"].append(module_id)
            visitor = _PythonSymbolVisitor(source, module_id)
            visitor.visit(tree)
            symbols.extend(visitor.symbols)
            for name, symbol_ids in visitor.by_name.items():
                by_name[name].extend(symbol_ids)
            for caller, callees in visitor.calls.items():
                calls_by_symbol[caller].extend(callees)
            for source_symbol, referenced_names in visitor.references.items():
                references_by_symbol[source_symbol].extend(referenced_names)
        edges: list[dict[str, Any]] = []
        for caller, called_names in sorted(calls_by_symbol.items()):
            for called_name in sorted(set(called_names)):
                candidates = sorted(by_name.get(called_name, []))
                if not candidates:
                    continue
                target = candidates[0]
                edges.append(
                    {
                        "source_symbol_id": caller,
                        "target_symbol_id": target,
                        "edge_kind": "CALLS",
                        "metadata": {"resolved_name": called_name},
                    }
                )
        call_pairs = {
            (edge["source_symbol_id"], edge["target_symbol_id"])
            for edge in edges
            if edge["edge_kind"] == "CALLS"
        }
        for source_symbol, referenced_names in sorted(references_by_symbol.items()):
            for referenced_name in sorted(set(referenced_names)):
                candidates = sorted(by_name.get(referenced_name, []))
                if not candidates:
                    continue
                target = candidates[0]
                if source_symbol == target or (source_symbol, target) in call_pairs:
                    continue
                edges.append(
                    {
                        "source_symbol_id": source_symbol,
                        "target_symbol_id": target,
                        "edge_kind": "REFERENCES",
                        "metadata": {"resolved_name": referenced_name},
                    }
                )
        symbols.sort(key=lambda item: item["symbol_id"])
        edges.sort(
            key=lambda item: (
                item["source_symbol_id"], item["target_symbol_id"], item["edge_kind"]
            )
        )
        return symbols, edges


class _PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self, source: Mapping[str, Any], module_id: str):
        self.source = source
        self.module_id = module_id
        self.stack: list[tuple[str, str]] = []
        self.symbols: list[dict[str, Any]] = []
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.calls: dict[str, list[str]] = defaultdict(list)
        self.references: dict[str, list[str]] = defaultdict(list)
        self.current_symbol = module_id

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self._visit_definition(node, "CLASS")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_definition(node, "METHOD" if self.stack and self.stack[-1][1] == "CLASS" else "FUNCTION")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_definition(node, "METHOD" if self.stack and self.stack[-1][1] == "CLASS" else "FUNCTION")

    def visit_Call(self, node: ast.Call) -> Any:
        name = _call_name(node.func)
        if name:
            self.calls[self.current_symbol].append(name.split(".")[-1])
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        if isinstance(node.ctx, ast.Load):
            self.references[self.current_symbol].append(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if isinstance(node.ctx, ast.Load):
            self.references[self.current_symbol].append(node.attr)
        self.generic_visit(node)

    def _visit_definition(self, node: Any, kind: str) -> None:
        qualified_parts = [item[0] for item in self.stack] + [node.name]
        qualified_name = ".".join(qualified_parts)
        symbol_id = _symbol_id(
            self.source["revision_id"], self.source["title"], qualified_name, node.lineno
        )
        signature = _signature(node) if kind in {"FUNCTION", "METHOD"} else None
        self.symbols.append(
            {
                "symbol_id": symbol_id,
                "source_revision_id": self.source["revision_id"],
                "file_path": _source_file_path(self.source),
                "name": node.name,
                "qualified_name": qualified_name,
                "symbol_kind": kind,
                "start_line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "signature": signature,
                "index_version": CODE_INDEX_VERSION,
            }
        )
        self.by_name[node.name].append(symbol_id)
        previous = self.current_symbol
        self.current_symbol = symbol_id
        self.stack.append((node.name, kind))
        self.generic_visit(node)
        self.stack.pop()
        self.current_symbol = previous


def _record_updated_at(
    record: Mapping[str, Any], projection: Mapping[str, Any]
) -> str:
    document = record.get("document")
    if isinstance(document, Mapping):
        for key in (
            "updated_at",
            "resolved_at",
            "recorded_at",
            "opened_at",
            "created_at",
            "asserted_at",
            "captured_at",
            "linked_at",
        ):
            value = document.get(key)
            if isinstance(value, str):
                return value
    history_sequences = record.get("history_sequences")
    if isinstance(history_sequences, Sequence) and history_sequences:
        latest = max(int(item) for item in history_sequences)
        for entry in projection["ledger"]["entries"]:
            if int(entry["sequence"]) == latest:
                return str(entry["committed_at"])
    entries = projection["ledger"]["entries"]
    return (
        str(entries[-1]["committed_at"])
        if entries
        else "1970-01-01T00:00:00Z"
    )


def _rrf(*rankings: Sequence[str], constant: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for index, document_id in enumerate(ranking, start=1):
            scores[document_id] += 1.0 / (constant + index)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _symbol_id(revision_id: str, file_path: str, qualified_name: str, line: int) -> str:
    digest = sha256_json(
        {
            "revision_id": revision_id,
            "file_path": file_path,
            "qualified_name": qualified_name,
            "line": line,
        }
    ).split(":", 1)[1]
    return f"symbol_{digest[:32]}"


def _source_file_path(source: Mapping[str, Any]) -> str:
    locator = str(source.get("source_locator", ""))
    if locator.startswith("file:"):
        try:
            return Path(locator.removeprefix("file://")).as_posix()
        except (OSError, ValueError):
            pass
    return str(source["title"])


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    names = [argument.arg for argument in node.args.posonlyargs]
    names.extend(argument.arg for argument in node.args.args)
    if node.args.vararg:
        names.append("*" + node.args.vararg.arg)
    names.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg:
        names.append("**" + node.args.kwarg.arg)
    return f"{node.name}({', '.join(names)})"


__all__ = [
    "CODE_INDEX_VERSION",
    "RETRIEVAL_INDEX_VERSION",
    "RetrievalBuildReport",
    "RetrievalError",
    "RetrievalService",
    "VectorRanker",
]
