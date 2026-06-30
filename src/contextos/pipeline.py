"""The request pipeline.

SecurityContext -> trace -> **semantic cache (short-circuits on hit)** -> memory retrieve ->
**context assembly: rank + budget-pack + edge-load + inject** -> **model router (cost/quality/
latency utility + circuit-breaker fallback chain)** -> backend adapter -> response, with a
decision record per stage. A cache hit skips everything downstream; misses store the
(non-memory-grounded) response. When no router/registry is wired, a single adapter is used.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from .adapters.base import BackendAdapter, ChatMessage, ChatRequest, ChatResponse, Role, Usage
from .assembler.budget import TokenBudget
from .assembler.engine import ContextAssembler, ContextSources
from .assembler.tokenizer import HeuristicTokenizer, Tokenizer
from .assembler.weights import RankWeights
from .cache.engine import SemanticCache
from .compressor.engine import ContextCompressor
from .memory.engine import MemoryEngine
from .models.common import Action
from .models.memory import MemoryCandidate
from .observability.cost import CostLedger
from .observability.tracer import Trace, Tracer
from .replay.bundle import BundleMessage, render_prompt_hash
from .replay.engine import ReplayDebugger
from .router.engine import BackendRegistry, ModelRouter
from .router.types import BackendUnavailable, RouteDecision
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
        compressor: ContextCompressor | None = None,
        replay: ReplayDebugger | None = None,
        router: ModelRouter | None = None,
        backends: BackendRegistry | None = None,
        cost: CostLedger | None = None,
        default_model: str | None = None,
        window_tokens: int = 6000,
        compress_target_tokens: int = 256,
        weights: RankWeights | None = None,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        self._adapter = adapter
        self._tracer = tracer or Tracer()
        self._policy = policy or PolicyEngine()
        self._memory = memory
        self._assembler = assembler
        self._cache = cache
        self._compressor = compressor
        self._replay = replay
        self._router = router
        self._backends = backends
        self._cost = cost
        self._default_model = default_model or "default"
        self._compress_target = compress_target_tokens
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

        # Compression runs AFTER retrieval/ACL and BEFORE assembly (pipeline invariant). The hot
        # path uses the deterministic extractive tier; the abstractive (LLM) tier is opt-in.
        with trace.span("compress", "context-compression") as sp:
            if self._compressor is not None and candidates:
                saved = 0
                compressed: list[MemoryCandidate] = []
                for c in candidates:
                    block = await self._compressor.compress(
                        c.content, self._compress_target, self._tokenizer, query=prompt
                    )
                    if block.compressed_tokens < block.original_tokens:
                        saved += block.original_tokens - block.compressed_tokens
                        compressed.append(c.model_copy(update={"content": block.text}))
                    else:
                        compressed.append(c)
                candidates = compressed
                Trace.record(sp, f"compressed candidates; saved ~{saved} tok", saved_tokens=str(saved))
            else:
                Trace.record(sp, "skipped (no compressor or no candidates)")

        messages: list[ChatMessage] = []
        injected = 0
        assembled_tokens = 0
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
                assembled_tokens = assembled.used_tokens

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
                assembled_tokens = self._tokenizer.count(prompt) + self._tokenizer.count(system or "")
                Trace.record(sp, "passthrough (no assembler configured)")

        decision: RouteDecision | None = None
        with trace.span("route", "model-router") as sp:
            if self._router is not None and self._backends is not None and len(self._backends):
                # NoEligibleBackend propagates -> gateway maps it to HTTP 503 (fail-closed).
                decision = self._router.route(
                    ctx, prompt, assembled_tokens=assembled_tokens, registry=self._backends
                )
                chosen_model = decision.model_id
                Trace.record(
                    sp, f"routed -> {decision.backend} ({chosen_model})",
                    backend=decision.backend, model=chosen_model,
                    utility=f"{decision.utility:.4f}", difficulty=f"{decision.difficulty:.2f}",
                    from_safe_default=str(decision.from_safe_default),
                    fallback=",".join(decision.fallback_chain),
                )
            else:
                Trace.record(sp, f"single-backend -> {self._adapter.name}", model=chosen_model)

        with trace.span("backend.invoke", "adapter.generate") as sp:
            if decision is not None:
                resp, used, chosen_model = await self._invoke_routed(decision, messages, max_tokens)
            else:
                resp = await self._adapter.generate(
                    ChatRequest(model=chosen_model, messages=messages, max_tokens=max_tokens)
                )
                used = self._adapter.name
            Trace.record(
                sp,
                f"generated via {used}",
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
            if self._cost is not None and committed:
                self._cost.record(ctx, chosen_model, resp.usage)
            Trace.record(
                sp,
                "committed" if committed else "discarded (no terminal event)",
                cached=str(cached), non_cacheable=str(injected > 0),
            )

        return ChatResult(text=resp.text, trace_id=trace.trace_id, model=resp.model, usage=resp.usage)

    async def _invoke_routed(
        self, decision: RouteDecision, messages: list[ChatMessage], max_tokens: int
    ) -> tuple[ChatResponse, str, str]:
        """Dispatch to the chosen backend, walking the fallback chain and tripping breakers."""
        backends = self._backends
        if backends is None:  # pragma: no cover - only called once a routing decision exists
            raise BackendUnavailable("no backend registry configured")
        last_exc: Exception | None = None
        for name in [decision.backend, *decision.fallback_chain]:
            entry = backends.get(name)
            if entry is None or not entry.breaker.allows():
                continue
            try:
                resp = await entry.adapter.generate(
                    ChatRequest(model=entry.spec.model_id, messages=messages, max_tokens=max_tokens)
                )
                entry.breaker.record_success()
                return resp, name, entry.spec.model_id
            except Exception as exc:  # adapter/network failure -> trip breaker, try the next backend
                entry.breaker.record_failure()
                last_exc = exc
        raise BackendUnavailable("all routed backends failed or are breaker-open") from last_exc
