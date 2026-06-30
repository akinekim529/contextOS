"""ContextOS — the context & memory operating system for LLM applications.

The two-line path (design philosophy #5, progressive disclosure):

    from contextos import ContextOS

    ctx = ContextOS(user_id="123", tenant="acme")
    response = ctx.chat("how do I deploy an LLM on Kubernetes?")
    print(response)            # response is printable; response.trace_id is replayable

Power users get more on the same object: ``remember`` (memory), ``replay`` (flagship),
``diff`` (context diff), ``commit``/``branch``/``rollback`` (memory versioning),
``consolidate`` (worker plane), and ``cost`` (the ledger). The SDK and the REST API share the
exact same machinery, so they never diverge.
"""

from __future__ import annotations

import asyncio

from .adapters.base import BackendAdapter
from .config.settings import ContextOSSettings
from .gateway.app import (
    build_adapter,
    build_assembler,
    build_cache,
    build_compressor,
    build_cost_ledger,
    build_memory_engine,
    build_replay,
    build_versioning,
)
from .memory.engine import MemoryEngine
from .models.common import MemoryTier
from .observability.cost import CostSummary
from .pipeline import ChatResult, Pipeline
from .replay.diff import ContextDiff, diff_bundles
from .replay.engine import ReplayResult
from .security.context import SecurityContext
from .versioning.engine import MemoryDiff
from .workers.consolidation import consolidate_namespace
from .workers.runner import BackgroundRunner

__version__ = "0.1.0"
__all__ = [
    "ChatResult",
    "ContextDiff",
    "ContextOS",
    "CostSummary",
    "MemoryDiff",
    "ReplayResult",
    "__version__",
]


class ContextOS:
    """In-process client bound to one user+tenant. Sync methods run the async pipeline to
    completion; for a long-lived async app, drive :class:`Pipeline` directly."""

    def __init__(
        self,
        *,
        user_id: str,
        tenant: str,
        namespace: str | None = None,
        settings: ContextOSSettings | None = None,
        adapter: BackendAdapter | None = None,
    ) -> None:
        self._settings = settings or ContextOSSettings()
        self._ctx = SecurityContext.resolve(tenant_id=tenant, user_id=user_id, namespace=namespace)
        self._adapter = adapter or build_adapter(self._settings)
        self._memory: MemoryEngine = build_memory_engine(self._settings)
        self._cost = build_cost_ledger(self._settings)
        self._versioning = build_versioning(self._memory)
        self._runner = BackgroundRunner()
        assembler = build_assembler(self._settings)
        self._replay = build_replay(self._settings, assembler)
        self._pipeline = Pipeline(
            adapter=self._adapter,
            memory=self._memory,
            assembler=assembler,
            cache=build_cache(self._settings),
            compressor=build_compressor(self._settings),
            replay=self._replay,
            cost=self._cost,
            default_model=self._settings.default_model,
            window_tokens=self._settings.default_token_budget,
        )

    def chat(self, prompt: str, *, model: str | None = None, max_tokens: int = 512,
             system: str | None = None) -> ChatResult:
        return asyncio.run(
            self._pipeline.run(self._ctx, prompt, model=model, max_tokens=max_tokens, system=system)
        )

    def remember(self, content: str, *, tier: MemoryTier = MemoryTier.SEMANTIC,
                 importance: float = 0.5) -> str:
        """Persist a memory for this user+tenant; returns its id. Later ``chat`` calls retrieve it."""
        mem = asyncio.run(self._memory.write(self._ctx, content, tier=tier, importance=importance))
        return mem.id

    def replay(self, trace_id: str) -> ReplayResult | None:
        """Reproduce a past decision: rebuild the exact prompt and assert it matches bit-for-bit."""
        return asyncio.run(self._replay.replay(self._ctx, trace_id))

    def diff(self, trace_a: str, trace_b: str) -> ContextDiff | None:
        """Diff two requests' context bundles (candidates, model, prompt)."""
        a = self._replay.get(self._ctx, trace_a)
        b = self._replay.get(self._ctx, trace_b)
        return diff_bundles(a, b) if a is not None and b is not None else None

    def consolidate(self) -> int:
        """Summarize this namespace's memories into one durable fact, via the worker plane."""

        async def _run() -> int:
            result = 0

            async def job() -> None:
                nonlocal result
                result = await consolidate_namespace(self._memory, self._adapter, self._ctx)

            self._runner.enqueue(job)
            await self._runner.drain()
            return result

        return asyncio.run(_run())

    def commit(self, label: str = "", *, branch: str = "main") -> str:
        """Snapshot the current memory state; returns the commit id."""
        return asyncio.run(self._versioning.commit(self._ctx, label, branch=branch))

    def branch(self, name: str, *, from_branch: str = "main") -> None:
        self._versioning.branch(self._ctx, name, from_branch=from_branch)

    def branches(self) -> dict[str, str | None]:
        return self._versioning.branches(self._ctx)

    def memory_diff(self, a: str, b: str) -> MemoryDiff:
        """Diff two memory commits (added/removed/changed memory ids)."""
        return self._versioning.diff(self._ctx, a, b)

    def rollback(self, cid: str) -> int:
        """Restore memories from a commit that are currently missing; returns # restored."""
        return asyncio.run(self._versioning.rollback(self._ctx, cid))

    def cost(self) -> CostSummary:
        """Per-model spend for this tenant."""
        return self._cost.summary(self._ctx)
