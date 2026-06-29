"""The request pipeline.

SecurityContext -> trace -> **semantic cache (short-circuits on hit)** -> memory retrieve ->
**context assembly: rank + budget-pack + edge-load + inject** -> model router (single backend)
-> backend adapter -> response, with a decision record per stage. A cache hit skips retrieval,
assembly, and the model call entirely; misses store the (non-memory-grounded) response.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from .adapters.base import BackendAdapter, ChatMessage, ChatRequest, Role, Usage
from .assembler.budget import TokenBudget
from .assembler.engine import ContextAssembler, ContextSources
from .assembler.tokenizer import HeuristicTokenizer, Tokenizer
from .assembler.weights import RankWeights
from .cache.engine import SemanticCache
from .memory.engine import MemoryEngine
from .models.common import Action
from .models.memory import MemoryCandidate
from .observability.tracer import Trace, Tracer
from .replay.bundle import BundleMessage, render_prompt_hash
from .replay.engine import ReplayDebugger
from .security.context import SecurityContext
from .security.rbac import PolicyEngine

DEFAULT_MMR_LAMBDA = 0.70  # passed to the assembler AND frozen in the replay bundle (must match)


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
        assembler: ContextAssembler | None = None,
        cache: SemanticCache | None = None,
        replay: ReplayDebugger | None = None,
        default_model: str | None = None,
        window_tokens: int = 6000,
        weights: RankWeights | None = None,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        self._adapter = adapter
        self._tracer = tracer or Tracer()
        self._policy = policy or PolicyEngine()
        self._memory = memory
        self._assembler = assembler
        self._cache = cache
        self._replay = replay
        self._default_model = default_model or "default"
        self._window_tokens = window_tokens
        self._weights = weights or RankWeights()
        self._tokenizer: Tokenizer = tokenizer or HeuristicTokenizer()

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

        chosen_model = model or self._default_model
        # Partition the cache by the actual system prompt without storing it in the key (C6).
        sysver = hashlib.sha256((system or "").encode("utf-8")).hexdigest()[:12]

        with trace.span("cache", "semantic-cache-lookup") as sp:
            cache_hit = (
                await self._cache.lookup(ctx, prompt, model_id=chosen_model, system_prompt_version=sysver)
                if self._cache is not None
                else None
            )
            if cache_hit is not None:
                Trace.record(
                    sp, f"hit ({cache_hit.tier})", verdict="hit",
                    tier=cache_hit.tier, similarity=f"{cache_hit.similarity:.3f}",
                )
            else:
                Trace.record(sp, "miss", verdict="miss", tier="none")

        if cache_hit is not None:
            # A hit short-circuits retrieval, assembly, routing, and the model call entirely.
            with trace.span("writeback", "served-from-cache") as sp:
                Trace.record(sp, "served from cache; no model call", source="cache")
            return ChatResult(
                text=cache_hit.response_text, trace_id=trace.trace_id, model=chosen_model, usage=Usage()
            )

        candidates: list[MemoryCandidate] = []
        with trace.span("retrieve", "memory-retrieve") as sp:
            if self._memory is not None:
                candidates = await self._memory.retrieve(ctx, prompt, k=12)
                top_ids = ",".join(c.memory_id for c in candidates[:5])
                Trace.record(
                    sp,
                    f"retrieved {len(candidates)} candidate(s) (raw scores)",
                    candidates=str(len(candidates)),
                    top_memory_ids=top_ids,
                )
            else:
                Trace.record(sp, "no memory engine configured", candidates="0")

        messages: list[ChatMessage] = []
        injected = 0
        with trace.span("assemble", "context-assembly") as sp:
            if self._assembler is not None:
                # Router picks the model before final packing -> correct tokenizer (C3).
                budget = TokenBudget(
                    window_tokens=self._window_tokens,
                    output_reserve=max_tokens,
                    system_reserve=self._tokenizer.count(system or ""),
                    latest_user_reserve=self._tokenizer.count(prompt),
                )
                sources = ContextSources(
                    system_prompt=system or "", latest_user_turn=prompt, candidates=candidates
                )
                # ContextOverflow propagates -> gateway maps it to HTTP 413 (fail-closed).
                assembled = await self._assembler.assemble(
                    budget, sources, self._weights, self._tokenizer, mmr_lambda=DEFAULT_MMR_LAMBDA
                )
                messages = assembled.messages
                injected = sum(1 for d in assembled.decisions if d.kept)

                # Flagship: freeze a content-addressed bundle so this decision is replayable.
                bundle_cid = "-"
                if self._replay is not None:
                    bundle = self._replay.capture(
                        trace.trace_id, ctx,
                        system_prompt=system or "", latest_user_turn=prompt, candidates=candidates,
                        weights=self._weights, mmr_lambda=DEFAULT_MMR_LAMBDA, budget=budget,
                        model_id=chosen_model,
                        rendered_messages=[
                            BundleMessage(role=m.role.value, content=m.content) for m in assembled.messages
                        ],
                        prompt_hash=render_prompt_hash(assembled.messages),
                    )
                    bundle_cid = bundle.bundle_cid

                Trace.record(
                    sp,
                    f"assembled {len(messages)} msg(s); {injected} memory block(s) injected",
                    used_tokens=str(assembled.used_tokens),
                    soft_budget=str(budget.soft_budget),
                    injected=str(injected),
                    bundle_cid=bundle_cid,
                )
            else:
                if system:
                    messages.append(ChatMessage(role=Role.SYSTEM, content=system))
                messages.append(ChatMessage(role=Role.USER, content=prompt))
                Trace.record(sp, "passthrough (no assembler configured)")

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
            cached = False
            if self._cache is not None and committed:
                # Memory-private-grounded responses are non-cacheable (C6).
                cached = await self._cache.store(
                    ctx, prompt, resp.text, model_id=chosen_model,
                    system_prompt_version=sysver, non_cacheable=injected > 0,
                )
            # Phase-2 bundle completion attach — only on a server terminal event (C8).
            if self._replay is not None and committed:
                self._replay.attach_output(ctx, trace.trace_id, resp.text)
            Trace.record(
                sp,
                "committed" if committed else "discarded (no terminal event)",
                cached=str(cached), non_cacheable=str(injected > 0),
            )

        return ChatResult(text=resp.text, trace_id=trace.trace_id, model=resp.model, usage=resp.usage)
