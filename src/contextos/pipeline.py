"""The request pipeline.

SecurityContext -> trace -> cache (no-op, Month 1) -> **memory retrieve (live when a
MemoryEngine is wired)** -> assembly (no-op, Month 1) -> model router (single backend) ->
backend adapter -> response, with a decision record per stage. Retrieved candidates are
recorded on the trace now; prompt *injection* arrives with the Assembler (next Month-1 step).
"""

from __future__ import annotations

from pydantic import BaseModel

from .adapters.base import BackendAdapter, ChatMessage, ChatRequest, Role, Usage
from .memory.engine import MemoryEngine
from .models.common import Action
from .observability.tracer import Trace, Tracer
from .security.context import SecurityContext
from .security.rbac import PolicyEngine


class ChatResult(BaseModel):
    text: str
    trace_id: str
    model: str
    usage: Usage

    def __str__(self) -> str:  # so the 2-line SDK path can ``print(response)``
        return self.text


class Pipeline:
    def __init__(
        self,
        *,
        adapter: BackendAdapter,
        tracer: Tracer | None = None,
        policy: PolicyEngine | None = None,
        memory: MemoryEngine | None = None,
        default_model: str | None = None,
    ) -> None:
        self._adapter = adapter
        self._tracer = tracer or Tracer()
        self._policy = policy or PolicyEngine()
        self._memory = memory
        self._default_model = default_model or "default"

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    async def run(
        self,
        ctx: SecurityContext,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 512,
        system: str | None = None,
    ) -> ChatResult:
        trace = self._tracer.start(ctx)

        with trace.span("auth", "resolve+authorize") as sp:
            # Routing a request *is* a policy decision — same authority as everything else (C10).
            self._policy.check(
                ctx, resource={"type": "model", "tenant_id": ctx.tenant_id}, action=Action.ROUTE
            )
            Trace.record(sp, "authorized", tenant=ctx.tenant_id, namespace=ctx.namespace)

        # --- Deferred stages (Month 1): kept as explicit, traced no-ops so the contract is real.
        with trace.span("cache", "semantic-cache-lookup") as sp:
            Trace.record(sp, "miss (cache engine lands Month 1)", verdict="miss")
        with trace.span("retrieve", "memory-retrieve") as sp:
            if self._memory is not None:
                candidates = await self._memory.retrieve(ctx, prompt, k=12)
                top_ids = ",".join(c.memory_id for c in candidates[:5])
                Trace.record(
                    sp,
                    f"retrieved {len(candidates)} candidate(s) (raw scores; assembler ranks next)",
                    candidates=str(len(candidates)),
                    top_memory_ids=top_ids,
                )
            else:
                Trace.record(sp, "no memory engine configured", candidates="0")
        with trace.span("assemble", "context-assembly") as sp:
            Trace.record(sp, "passthrough (assembler lands Month 1)", budget="n/a")

        chosen_model = model or self._default_model
        messages: list[ChatMessage] = []
        if system:
            messages.append(ChatMessage(role=Role.SYSTEM, content=system))
        messages.append(ChatMessage(role=Role.USER, content=prompt))

        with trace.span("route", "model-router") as sp:
            Trace.record(sp, f"single-backend skeleton -> {self._adapter.name}", model=chosen_model)

        with trace.span("backend.invoke", "adapter.generate") as sp:
            resp = await self._adapter.generate(
                ChatRequest(model=chosen_model, messages=messages, max_tokens=max_tokens)
            )
            Trace.record(
                sp,
                "generated",
                finish_reason=str(resp.finish_reason),
                prompt_tokens=str(resp.usage.prompt_tokens),
                completion_tokens=str(resp.usage.completion_tokens),
            )

        # Write-back commits only because a server-side finish_reason was reached (C8).
        with trace.span("writeback", "enqueue-async") as sp:
            committed = resp.finish_reason is not None
            Trace.record(sp, "committed" if committed else "discarded (no terminal event)")

        return ChatResult(text=resp.text, trace_id=trace.trace_id, model=resp.model, usage=resp.usage)
