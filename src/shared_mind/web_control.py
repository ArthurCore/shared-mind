"""Local-only review and governance control surface.

The HTTP layer delegates every operation to :class:`ProductService`; it never
opens SQLite directly and therefore cannot bypass Proposal or product-audit
boundaries.  The default and accepted bind addresses are loopback only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from .canonical import canonical_json
from .product import ProductError, ProductService
from .workspace import Workspace, WorkspaceError


MAX_BODY_BYTES = 1024 * 1024

_INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shared Mind</title><style>
body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#18181b}
h1{margin-bottom:.25rem}small{color:#52525b}pre{white-space:pre-wrap;background:#f4f4f5;padding:1rem;border-radius:.5rem;overflow:auto}
button{padding:.5rem .8rem;margin:.25rem}.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}@media(max-width:800px){.grid{grid-template-columns:1fr}}
</style></head><body><h1>Shared Mind</h1><small>Project has state. Agents come and go.</small>
<p><button onclick="load('/api/catalog','catalog')">Catalog</button><button onclick="load('/api/review-queue','queue')">Review queue</button><button onclick="load('/api/verify','verify')">Verify</button></p>
<div class="grid"><section><h2>Catalog</h2><pre id="catalog">Load catalog</pre></section><section><h2>Review queue</h2><pre id="queue">Load review queue</pre></section></div>
<h2>Integrity</h2><pre id="verify">Load verification</pre>
<script>async function load(url,id){const r=await fetch(url);document.getElementById(id).textContent=JSON.stringify(await r.json(),null,2)}</script></body></html>"""


class WebControlApplication:
    def __init__(self, service: ProductService):
        self.service = service

    def handle(
        self,
        method: str,
        target: str,
        body: bytes = b"",
    ) -> tuple[int, str, bytes]:
        split = urlsplit(target)
        path = split.path.rstrip("/") or "/"
        query = parse_qs(split.query, keep_blank_values=False)
        try:
            if method == "GET" and path == "/":
                return HTTPStatus.OK, "text/html; charset=utf-8", _INDEX.encode("utf-8")
            if method == "GET" and path == "/api/health":
                return self._json(HTTPStatus.OK, {"ok": True, "code": "HEALTHY"})
            if method == "GET" and path == "/api/catalog":
                return self._ok("CATALOG_READY", self.service.catalog())
            if method == "GET" and path == "/api/review-queue":
                return self._ok("REVIEW_QUEUE_READY", self.service.review_queue())
            if method == "GET" and path == "/api/verify":
                report = self.service.verify()
                return self._json(
                    HTTPStatus.OK if report["valid"] else HTTPStatus.CONFLICT,
                    {"ok": report["valid"], "code": "PRODUCT_INTEGRITY_VALID" if report["valid"] else "PRODUCT_INTEGRITY_INVALID", "data": report},
                )
            if method == "GET" and path == "/api/drafts":
                drafts = self.service.list_drafts(
                    status=_first(query, "status"),
                    draft_kind=_first(query, "kind"),
                    batch_id=_first(query, "batch_id"),
                )
                return self._ok("DRAFTS_LISTED", {"drafts": drafts, "count": len(drafts)})
            if method == "GET" and path.startswith("/api/drafts/"):
                return self._ok("DRAFT_SHOWN", self.service.get_draft(path.rsplit("/", 1)[1]))
            if method == "POST" and path == "/api/context":
                return self._ok("TASK_CONTEXT_READY", self.service.context(self._object(body)))
            if method == "POST" and path == "/api/search":
                values = self._object(body)
                return self._ok(
                    "SEARCH_COMPLETED",
                    self.service.search(
                        str(values.get("query", "")),
                        kinds=tuple(values.get("kinds", [])),
                        limit=int(values.get("limit", 20)),
                    ),
                )
            if method == "POST" and path == "/api/tool":
                values = self._object(body)
                arguments = values.get("arguments", {})
                if not isinstance(arguments, Mapping):
                    raise ProductError(
                        "TOOL_ARGUMENTS_INVALID", "arguments must be an object"
                    )
                return self._ok(
                    "MEMORY_TOOL_COMPLETED",
                    self.service.tool_call(str(values.get("name", "")), arguments),
                )
            if method == "POST" and path == "/api/build":
                values = self._object(body)
                target_name = str(values.get("target", "all"))
                if target_name not in {"views", "indexes", "all"}:
                    raise ProductError("BUILD_TARGET_INVALID", target_name)
                data: dict[str, Any] = {}
                if target_name in {"views", "all"}:
                    data["views"] = self.service.build_memory_views()
                if target_name in {"indexes", "all"}:
                    data["indexes"] = self.service.build_indexes()
                return self._ok("PRODUCT_VIEWS_BUILT", data)
            if method == "POST" and path.startswith("/api/skills/"):
                segments = path.split("/")
                if len(segments) != 6:
                    return self._not_found(path)
                skill_id, version_text, action = segments[3], segments[4], segments[5]
                version = int(version_text)
                values = self._object(body)
                if action == "mark-tested":
                    evidence = values.get("evidence")
                    if not isinstance(evidence, Mapping):
                        raise ProductError(
                            "SKILL_TEST_EVIDENCE_INVALID",
                            "evidence must be an object",
                        )
                    return self._ok(
                        "SKILL_TEST_RECORDED",
                        self.service.record_skill_test(
                            skill_id, version, evidence=evidence
                        ),
                    )
                if action == "approve":
                    approval = values.get("approval")
                    if not isinstance(approval, Mapping):
                        raise ProductError(
                            "SKILL_APPROVAL_INVALID",
                            "approval must be an object",
                        )
                    return self._ok(
                        "SKILL_APPROVED",
                        self.service.approve_skill(
                            skill_id, version, approval=approval
                        ),
                    )
            if method == "POST" and path.startswith("/api/drafts/"):
                segments = path.split("/")
                if len(segments) != 5:
                    return self._not_found(path)
                draft_id, action = segments[3], segments[4]
                values = self._object(body)
                if action == "commit":
                    return self._ok("DRAFT_COMMITTED", self.service.commit_draft(draft_id))
                if action == "reject":
                    return self._ok(
                        "DRAFT_REJECTED",
                        self.service.reject_draft(
                            draft_id, rationale=str(values.get("rationale", ""))
                        ),
                    )
                if action == "edit":
                    document = values.get("document")
                    if not isinstance(document, Mapping):
                        raise ProductError("DRAFT_DOCUMENT_INVALID", "document must be an object")
                    return self._ok(
                        "DRAFT_UPDATED",
                        self.service.edit_draft(
                            draft_id,
                            document,
                            expected_version=int(values["expected_version"]),
                        ),
                    )
            return self._not_found(path)
        except ProductError as exc:
            return self._json(
                _status_for_error(exc.code),
                {
                    "ok": False,
                    "code": exc.code,
                    "message": exc.message,
                    **({"data": exc.data} if exc.data is not None else {}),
                },
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "code": "REQUEST_INVALID", "message": str(exc)},
            )

    @staticmethod
    def _object(body: bytes) -> Mapping[str, Any]:
        if len(body) > MAX_BODY_BYTES:
            raise ProductError("REQUEST_TOO_LARGE", "Request body exceeds 1 MiB")
        value = json.loads(body.decode("utf-8") or "{}")
        if not isinstance(value, Mapping):
            raise ValueError("JSON body must be an object")
        return value

    @staticmethod
    def _json(status: int, document: Mapping[str, Any]) -> tuple[int, str, bytes]:
        return status, "application/json; charset=utf-8", (canonical_json(document) + "\n").encode("utf-8")

    def _ok(self, code: str, data: Any) -> tuple[int, str, bytes]:
        return self._json(HTTPStatus.OK, {"ok": True, "code": code, "data": data})

    def _not_found(self, path: str) -> tuple[int, str, bytes]:
        return self._json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "code": "ROUTE_NOT_FOUND", "message": path},
        )


def create_server(workspace: Workspace, *, host: str = "127.0.0.1", port: int = 8126) -> ThreadingHTTPServer:
    _require_loopback(host)
    service = ProductService(workspace)
    application = WebControlApplication(service)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SharedMindProduct/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length_text = self.headers.get("Content-Length", "0")
            try:
                length = int(length_text)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                status, content_type, body = application._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"ok": False, "code": "REQUEST_TOO_LARGE"},
                )
            else:
                body_bytes = self.rfile.read(length) if length else b""
                status, content_type, body = application.handle(
                    self.command, self.path, body_bytes
                )
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    original_close = server.server_close

    def close() -> None:
        try:
            service.close()
        finally:
            original_close()

    server.server_close = close  # type: ignore[method-assign]
    return server


def _require_loopback(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() != "localhost":
            raise ValueError("Shared Mind web control must bind to a loopback address")
        return
    if not address.is_loopback:
        raise ValueError("Shared Mind web control must bind to a loopback address")


def _first(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _status_for_error(code: str) -> int:
    if code.endswith("NOT_FOUND"):
        return HTTPStatus.NOT_FOUND
    if "VERSION" in code or code in {"DRAFT_FINAL", "DRAFT_NOT_EDITABLE"}:
        return HTTPStatus.CONFLICT
    if "INTEGRITY" in code or "HASH_MISMATCH" in code:
        return HTTPStatus.CONFLICT
    if code == "REQUEST_TOO_LARGE":
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    return HTTPStatus.BAD_REQUEST


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shared-mind-web")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8126)
    args = parser.parse_args(argv)
    workspace = Workspace.open(args.workspace)
    server = create_server(workspace, host=args.host, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["MAX_BODY_BYTES", "WebControlApplication", "create_server", "main"]
