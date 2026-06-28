"""ContextOS — the context & memory operating system for LLM applications.

The two-line path (design philosophy #5, progressive disclosure):

    from contextos import ContextOS

    ctx = ContextOS(user_id="123", tenant="acme")
    response = ctx.chat("how do I deploy an LLM on Kubernetes?")
    print(response)            # response is printable; response.trace_id is replayable

Everything underneath — security context, tracing, the model adapter — is the same machinery
the gateway uses, so the SDK and the REST API never diverge.
"""

from __future__ import annotations

import asyncio

from .adapters.base import BackendAdapter
from .config.settings import ContextOSSettings
from .gateway.app import build_adapter, build_memory_engine
from .memory.engine import MemoryEngine
from .models.common import MemoryTier
from .pipeline import ChatResult, Pipeline
from .security.context import SecurityContext

__version__ = "0.0.1"
__all__ = ["ChatResult", "ContextOS", "__version__"]


class ContextOS:
    """In-process client. Holds a pipeline bound to one user+tenant; ``chat`` runs the async
    pipeline to completion. For a long-lived async app, drive :class:`Pipeline` directly."""

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
        self._memory: MemoryEngine = build_memory_engine(self._settings)
        self._pipeline = Pipeline(
            adapter=adapter or build_adapter(self._settings),
            memory=self._memory,
            default_model=self._settings.default_model,
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
