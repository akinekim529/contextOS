"""The ContextOS gateway: a stateless FastAPI app exposing an OpenAI-shaped surface.

Walking-skeleton routes:
  POST /v1/chat            sync chat (SSE streaming lands Month 1)
  GET  /v1/traces/{id}     the trace stub — seed of the Replay Debugger
  GET  /healthz            liveness

The first thing every request does is resolve a SecurityContext from headers; if it cannot,
the request is denied before any work happens (fail-closed).
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..adapters.base import BackendAdapter
from ..adapters.fake import FakeAdapter
from ..adapters.openai_compatible import OpenAICompatibleAdapter
from ..adapters.vllm import vllm_adapter
from ..config.settings import ContextOSSettings
from ..pipeline import Pipeline
from ..security.context import SecurityContext
from ..security.errors import SecurityError
from .errors import envelope


class ChatBody(BaseModel):
    prompt: str
    model: str | None = None
    system: str | None = None
    max_tokens: int = 512


def build_adapter(settings: ContextOSSettings) -> BackendAdapter:
    kind = settings.backend_kind.lower()
    if kind == "fake":
        return FakeAdapter()
    if kind == "vllm":
        return vllm_adapter(base_url=settings.backend_base_url, model=settings.default_model,
                            api_key=settings.backend_api_key)
    # openai / tgi / ollama all speak the OpenAI-compatible wire format
    return OpenAICompatibleAdapter(base_url=settings.backend_base_url, model=settings.default_model,
                                   api_key=settings.backend_api_key, name=kind)


def resolve_security_context(
    x_tenant_id: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_namespace: str | None = Header(default=None),
) -> SecurityContext:
    """FastAPI dependency: build the request's SecurityContext from headers, or fail closed.

    Defined at module scope (not as a closure) so FastAPI can resolve the stringized
    ``from __future__ import annotations`` type hints against module globals.
    """
    # Raises MissingTenant/MissingNamespace (SecurityError) -> handled as 403 below.
    return SecurityContext.resolve(tenant_id=x_tenant_id, user_id=x_user_id, namespace=x_namespace)


def create_app(
    settings: ContextOSSettings | None = None, *, adapter: BackendAdapter | None = None
) -> FastAPI:
    settings = settings or ContextOSSettings()
    adapter = adapter or build_adapter(settings)
    pipeline = Pipeline(adapter=adapter, default_model=settings.default_model)

    app = FastAPI(title="ContextOS", version="0.0.1")
    app.state.pipeline = pipeline

    @app.exception_handler(SecurityError)
    async def _security_error(_: Request, exc: SecurityError) -> JSONResponse:
        return JSONResponse(status_code=403, content=envelope("access_denied", str(exc)))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/health")
    async def backend_health() -> dict[str, bool]:
        return {"backend_healthy": await adapter.health_check()}

    @app.post("/v1/chat")
    async def chat(
        body: ChatBody, ctx: SecurityContext = Depends(resolve_security_context)
    ) -> dict[str, Any]:
        result = await pipeline.run(
            ctx, body.prompt, model=body.model, max_tokens=body.max_tokens, system=body.system
        )
        return {
            "id": result.trace_id,
            "text": result.text,
            "model": result.model,
            "usage": result.usage.model_dump(),
            "trace_id": result.trace_id,
        }

    @app.get("/v1/traces/{trace_id}")
    async def get_trace(
        trace_id: str, ctx: SecurityContext = Depends(resolve_security_context)
    ) -> dict[str, Any]:
        trace = pipeline.tracer.get(ctx, trace_id)
        if trace is None:
            return JSONResponse(  # type: ignore[return-value]
                status_code=404, content=envelope("not_found", "trace not found", trace_id)
            )
        return {
            "trace_id": trace.trace_id,
            "tenant_id": trace.tenant_id,
            "spans": [
                {
                    "stage": s.stage,
                    "name": s.name,
                    "duration_ms": s.duration_ms(),
                    "decision": s.decision.model_dump() if s.decision else None,
                }
                for s in trace.spans
            ],
        }

    return app
