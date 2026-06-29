"""Semantic Cache v1 — two-tier (exact + semantic), per-tenant, fail-open.

Lookup: exact-hash tier first (embedding-free, <1ms); on a miss, embed the normalized query and
do a semantic-ANN search restricted to entries sharing the COARSE fingerprint (C6), accepting a
hit only at cosine >= tau (conservative default 0.92 — a wrong hit is worse than a miss).

Fail-open (design philosophy #7): any cache error degrades cost, never correctness or
availability — `lookup` returns a miss and `store` becomes a no-op rather than raising.

Cacheability (C6): responses grounded in a principal's private memory are flagged
non-cacheable by the caller and never stored — which is why the honest hit-ratio target is
25-45%, not higher.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..embedding.base import EmbeddingProvider
from ..models.cache import CacheEntry
from ..models.common import utcnow
from ..security.context import SecurityContext
from .backend import CacheBackend, StoredEntry
from .keys import exact_sig, normalize_query, prefilter_sig

DEFAULT_TAU = 0.92             # conservative cosine threshold (correctness over hit-rate)
DEFAULT_TTL_SECONDS = 86_400  # 24h for stable-fact responses


@dataclass(frozen=True)
class CacheHit:
    response_text: str
    tier: str          # "exact" | "semantic"
    similarity: float  # 1.0 for the exact tier
    key: str


class SemanticCache:
    def __init__(
        self,
        backend: CacheBackend,
        embedder: EmbeddingProvider,
        *,
        threshold: float = DEFAULT_TAU,
        default_ttl: int = DEFAULT_TTL_SECONDS,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._backend = backend
        self._embedder = embedder
        self._threshold = threshold
        self._default_ttl = default_ttl
        self._now = now

    async def lookup(
        self,
        ctx: SecurityContext,
        query: str,
        *,
        model_id: str,
        system_prompt_version: str = "v1",
        stable_facts_version: str = "v0",
    ) -> CacheHit | None:
        try:
            pre = prefilter_sig(
                ctx.tenant_id, ctx.namespace, model_id, system_prompt_version, stable_facts_version
            )
            ekey = exact_sig(
                ctx.tenant_id, ctx.namespace, model_id, system_prompt_version, stable_facts_version, query
            )
            exact = await self._backend.get_exact(ctx.tenant_id, ekey)
            if exact is not None:
                return CacheHit(exact.response_text, "exact", 1.0, ekey)

            emb = await self._embedder.embed(normalize_query(query))
            sem = await self._backend.search_semantic(ctx.tenant_id, pre, emb, self._threshold)
            if sem is not None:
                stored, sim = sem
                return CacheHit(stored.response_text, "semantic", sim, stored.entry.key)
            return None
        except Exception:
            return None  # fail-open: a cache failure never breaks the request

    async def store(
        self,
        ctx: SecurityContext,
        query: str,
        response_text: str,
        *,
        model_id: str,
        system_prompt_version: str = "v1",
        stable_facts_version: str = "v0",
        non_cacheable: bool = False,
        ttl_seconds: int | None = None,
    ) -> bool:
        if non_cacheable:
            return False  # memory-private-grounded responses are never cached (C6)
        try:
            pre = prefilter_sig(
                ctx.tenant_id, ctx.namespace, model_id, system_prompt_version, stable_facts_version
            )
            ekey = exact_sig(
                ctx.tenant_id, ctx.namespace, model_id, system_prompt_version, stable_facts_version, query
            )
            emb = await self._embedder.embed(normalize_query(query))
            ttl = ttl_seconds or self._default_ttl
            entry = CacheEntry(
                key=ekey, tenant_id=ctx.tenant_id, fingerprint=pre, response_ref=ekey,
                model_id=model_id, ttl_seconds=ttl, non_cacheable=False,
            )
            stored = StoredEntry(
                entry=entry, embedding=emb, response_text=response_text,
                expires_at=self._now() + timedelta(seconds=ttl),
            )
            await self._backend.put(stored)
            return True
        except Exception:
            return False  # fail-open
