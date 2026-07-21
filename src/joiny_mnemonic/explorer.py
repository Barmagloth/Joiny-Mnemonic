from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .service import MemoryService


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class AppendRequest(StrictModel):
    kind: str = "message"
    content: str
    role: str | None = "user"
    branch_id: str = "main"
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list)


class SearchRequest(StrictModel):
    query: str
    branch_id: str = "main"
    limit: int = Field(default=10, ge=1, le=100)
    exact: bool = False
    include_events: bool = True
    semantic: bool = True


class ResumeRequest(StrictModel):
    branch_id: str = "main"
    token_budget: int = Field(default=1500, ge=1, le=1500)
    query: str = "resume current goal constraints decisions and open tasks"
    session_id: str | None = None


def create_explorer_app(service: MemoryService) -> FastAPI:
    app = FastAPI(
        title="Joiny-Mnemonic Dataflow Explorer",
        version="1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        body = exc.body if isinstance(exc.body, dict) else {"body": exc.body}
        flow = service.dataflow.begin(
            "http_validation", source="fastapi",
            branch_id=str(body.get("branch_id", "main")),
            input_value={"path": request.url.path, "body": body},
        )
        flow.step(
            "boundary.validation", input_value=body,
            output_value={"errors": exc.errors()},
            decision={"accepted": False, "extra_fields_forbidden": True, "strict": True},
            status="failed",
        )
        flow.fail(ValueError("request schema validation failed"))
        return JSONResponse(status_code=400, content={"detail": exc.errors()})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        html = Path(__file__).with_name("explorer.html").read_text(encoding="utf-8")
        return html.replace("&middot;", chr(183)).replace("&hellip;", chr(8230))

    @app.get("/v1/dataflow/status")
    def status() -> dict[str, Any]:
        return {
            "enabled": True,
            "database": str(service.store.path),
            "recorder_errors": list(service.dataflow.errors),
            "external_sinks": len(service.dataflow.sinks),
            "opentelemetry": "not_installed_extension_point_available",
        }

    @app.get("/v1/dataflow/operations")
    def operations(
        branch: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> Any:
        return service.store.list_dataflow_operations(branch_id=branch, limit=limit)

    @app.get("/v1/dataflow/operations/{operation_id}")
    def operation(operation_id: str) -> Any:
        try:
            return service.store.get_dataflow_operation(operation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/dataflow/entries")
    def entries(
        branch: str | None = None,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> Any:
        return service.store.list_dataflow_entries(
            branch_id=branch, after_seq=after_seq, limit=limit
        )

    @app.post("/v1/events")
    def append(request: AppendRequest) -> Any:
        return service.append_event(**request.model_dump())

    @app.post("/v1/search")
    def search(request: SearchRequest) -> Any:
        return service.search(**request.model_dump())

    @app.post("/v1/resume")
    def resume(request: ResumeRequest) -> Any:
        return service.resume(**request.model_dump())

    return app


def serve_explorer(
    service: MemoryService, host: str = "127.0.0.1", port: int = 8766
) -> None:
    import uvicorn

    uvicorn.run(create_explorer_app(service), host=host, port=port, log_level="warning")
