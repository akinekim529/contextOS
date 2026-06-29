"""Cache backend abstraction + dependency-free reference implementation.

The cache is NOT a vector database (scope boundary, §2.4): it is a bounded, TTL-evicted set of
prior responses per tenant. The reference backend keeps them in memory; production uses a Redis
exact tier + a pgvector semantic tier behind this same interface. Isolation is physical — each
tenant has its own dict, so a lookup cannot even see another tenant's entries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..memory.scoring import cosine
from ..models.cache import CacheEntry
from ..models.common import utcnow


@dataclass
class StoredEntry:
    entry: CacheEntry            # metadata (tenant-salted key, fingerprint pre-filter, model, ttl)
    embedding: list[float]       # query embedding for the semantic tier
    response_text: str
    expires_at: datetime


@runtime_checkable
class CacheBackend(Protocol):
    async def get_exact(self, tenant_id: str, key: str) -> StoredEntry | None: ...

    async def search_semantic(
        self, tenant_id: str, prefilter: str, embedding: list[float], threshold: float
    ) -> tuple[StoredEntry, float] | None: ...

    async def put(self, stored: StoredEntry) -> None: ...

    async def health_check(self) -> bool: ...


class InMemoryCacheBackend:
    def __init__(self, *, now: Callable[[], datetime] = utcnow) -> None:
        self._by_tenant: dict[str, dict[str, StoredEntry]] = {}
        self._now = now

    def _live(self, s: StoredEntry) -> bool:
        return self._now() < s.expires_at  # lazy TTL expiry on read

    async def get_exact(self, tenant_id: str, key: str) -> StoredEntry | None:
        s = self._by_tenant.get(tenant_id, {}).get(key)
        return s if (s is not None and self._live(s)) else None

    async def search_semantic(
        self, tenant_id: str, prefilter: str, embedding: list[float], threshold: float
    ) -> tuple[StoredEntry, float] | None:
        best: StoredEntry | None = None
        best_sim = -1.0
        for s in self._by_tenant.get(tenant_id, {}).values():
            if not self._live(s) or s.entry.fingerprint != prefilter:
                continue  # hard pre-filter: same model + versions only (C6)
            sim = cosine(embedding, s.embedding)
            if sim >= threshold and sim > best_sim:
                best, best_sim = s, sim
        return (best, best_sim) if best is not None else None

    async def put(self, stored: StoredEntry) -> None:
        self._by_tenant.setdefault(stored.entry.tenant_id, {})[stored.entry.key] = stored

    async def health_check(self) -> bool:
        return True
