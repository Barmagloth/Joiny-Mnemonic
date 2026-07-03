from __future__ import annotations

import base64
import json
from dataclasses import asdict, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .service import MemoryService


MAX_BODY_BYTES = 16 * 1024 * 1024


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot JSON encode {type(value).__name__}")


def make_handler(service: MemoryService) -> type[BaseHTTPRequestHandler]:
    class MemoryRequestHandler(BaseHTTPRequestHandler):
        server_version = "Joiny-Mnemonic/0.4"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, value: Any) -> None:
            data = json.dumps(
                value, ensure_ascii=False, default=_json_default, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY_BYTES:
                raise ValueError("request body is too large")
            raw = self.rfile.read(length)
            value = json.loads(raw or b"{}")
            if not isinstance(value, dict):
                raise ValueError("JSON request body must be an object")
            return value

        def _dispatch_error(self, exc: Exception) -> None:
            if isinstance(exc, KeyError):
                status = HTTPStatus.NOT_FOUND
            elif isinstance(exc, (ValueError, TypeError)):
                status = HTTPStatus.BAD_REQUEST
            else:
                status = HTTPStatus.INTERNAL_SERVER_ERROR
            self._send(status, {"error": type(exc).__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == "/v1/health":
                    self._send(HTTPStatus.OK, service.verify())
                    return
                if parsed.path == "/v1/capabilities":
                    agent = query.get("agent", [None])[0]
                    self._send(HTTPStatus.OK, service.capabilities(agent))
                    return
                if parsed.path == "/v1/timeline":
                    branch = query.get("branch", ["main"])[0]
                    limit = int(query.get("limit", ["50"])[0])
                    self._send(
                        HTTPStatus.OK,
                        service.retrieval.timeline(branch_id=branch, limit=limit),
                    )
                    return
                if parsed.path == "/v1/usage":
                    self._send(
                        HTTPStatus.OK,
                        service.usage.report(
                            branch_id=query.get("branch", ["main"])[0],
                            session_id=query.get("session", [None])[0],
                        ),
                    )
                    return
                if parsed.path == "/v1/tasks":
                    self._send(
                        HTTPStatus.OK,
                        service.tasks.list(status=query.get("status", [None])[0]),
                    )
                    return
                if parsed.path.startswith("/v1/tool-output-views/"):
                    self._send(
                        HTTPStatus.OK,
                        service.store.list_tool_output_views(parsed.path.rsplit("/", 1)[-1]),
                    )
                    return
                if parsed.path == "/v1/project-source":
                    relative_path = query.get("path", [None])[0]
                    if relative_path is None:
                        raise ValueError("path query parameter is required")
                    expected_hash = query.get("expected_hash", [None])[0]
                    self._send(
                        HTTPStatus.OK,
                        service.project_source(relative_path, expected_hash=expected_hash),
                    )
                    return
                if parsed.path == "/v1/code/search":
                    value = query.get("query", [None])[0]
                    if value is None:
                        raise ValueError("query parameter is required")
                    self._send(HTTPStatus.OK, service.code.search(value, limit=int(query.get("limit", ["20"])[0])))
                    return
                if parsed.path == "/v1/code/context":
                    symbol = query.get("symbol", [None])[0]
                    if symbol is None:
                        raise ValueError("symbol parameter is required")
                    self._send(HTTPStatus.OK, service.code.context(symbol))
                    return
                if parsed.path == "/v1/code/impact":
                    symbol = query.get("symbol", [None])[0]
                    if symbol is None:
                        raise ValueError("symbol parameter is required")
                    self._send(HTTPStatus.OK, service.code.impact(symbol, depth=int(query.get("depth", ["3"])[0])))
                    return
                if parsed.path.startswith("/v1/events/"):
                    self._send(
                        HTTPStatus.OK,
                        service.store.get_event(parsed.path.rsplit("/", 1)[-1]),
                    )
                    return
                if parsed.path.startswith("/v1/source/"):
                    self._send(
                        HTTPStatus.OK,
                        service.exact_source(parsed.path.rsplit("/", 1)[-1]),
                    )
                    return
                if parsed.path.startswith("/v1/artifacts/"):
                    artifact = service.store.get_artifact(parsed.path.rsplit("/", 1)[-1])
                    value = asdict(artifact)
                    value["data_base64"] = base64.b64encode(value.pop("data")).decode("ascii")
                    self._send(HTTPStatus.OK, value)
                    return
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except Exception as exc:
                self._dispatch_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                body = self._body()
                if path == "/v1/sessions":
                    result = {
                        "id": service.store.start_session(
                            body["agent"],
                            branch_id=body.get("branch_id", "main"),
                            capabilities=body.get("capabilities"),
                        )
                    }
                elif path == "/v1/branches":
                    result = {
                        "id": service.store.create_branch(
                            body["id"],
                            parent_id=body.get("parent_id", "main"),
                            fork_event_seq=body.get("fork_event_seq"),
                        )
                    }
                elif path == "/v1/events":
                    result = service.store.append_event(**body)
                elif path == "/v1/artifacts":
                    values = dict(body)
                    if "data_base64" in values:
                        values["data"] = base64.b64decode(values.pop("data_base64"), validate=True)
                    result = service.store.append_artifact(**values)
                elif path == "/v1/blocks":
                    result = service.store.set_active_block(**body)
                elif path == "/v1/memories":
                    result = service.derive_memory(**body)
                elif path == "/v1/search":
                    result = service.search(**body)
                elif path == "/v1/graph/neighbors":
                    values = dict(body)
                    entity = values.pop("entity")
                    result = service.knowledge_neighbors(entity, **values)
                elif path == "/v1/snapshots":
                    result = service.create_snapshot(**body)
                elif path == "/v1/resume":
                    result = service.resume(**body)
                elif path == "/v1/prompt":
                    result = service.prompts.assemble(**body)
                elif path == "/v1/budget-policy":
                    values = dict(body)
                    agent = values.pop("agent", None)
                    profile = values.pop("profile", None)
                    if agent:
                        config_path, policy = service.context_limits.configure_agent(
                            str(agent), profile=profile, overrides=values
                        )
                        result = {"path": str(config_path), "policy": policy}
                    else:
                        result = service.store.set_budget_policy(**values)
                elif path == "/v1/governor":
                    values = dict(body)
                    apply = bool(values.pop("apply", False))
                    result = (
                        service.governor.evaluate_and_apply(**values)
                        if apply else service.governor.decide(**values)
                    )
                elif path == "/v1/tasks":
                    result = service.tasks.start(
                        body["task_key"],
                        body["title"],
                        parent_branch=body.get("parent_branch", "main"),
                        parent_task_key=body.get("parent_task_key"),
                        session_id=body.get("session_id"),
                        metadata=body.get("metadata"),
                    )
                elif path.startswith("/v1/tasks/") and path.endswith("/status"):
                    task_key = path.split("/")[-2]
                    result = service.tasks.set_status(
                        task_key,
                        body["status"],
                        note=body.get("note", ""),
                        session_id=body.get("session_id"),
                        metadata=body.get("metadata"),
                    )
                elif path.startswith("/v1/tasks/") and path.endswith("/resume"):
                    task_key = path.split("/")[-2]
                    result = service.tasks.resume(
                        task_key,
                        token_budget=body.get("token_budget", 1500),
                        query=body.get("query"),
                    )
                elif path == "/v1/consolidate":
                    result = service.consolidate(**body)
                elif path == "/v1/compact":
                    result = service.compact(**body)
                elif path.startswith("/v1/ingest/"):
                    result = service.ingest_native(
                        path.rsplit("/", 1)[-1],
                        body["event"],
                        branch_id=body.get("branch_id", "main"),
                        session_id=body.get("session_id"),
                    )
                else:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                self._send(HTTPStatus.OK, result)
            except Exception as exc:
                self._dispatch_error(exc)

    return MemoryRequestHandler


def serve(service: MemoryService, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the local API. Binding beyond loopback should be protected by a proxy."""
    server = ThreadingHTTPServer((host, port), make_handler(service))
    try:
        server.serve_forever()
    finally:
        server.server_close()
