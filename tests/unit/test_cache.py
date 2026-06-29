"""Semantic Cache v1: exact + semantic tiers, tenant isolation, TTL, fail-open, partitioning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contextos.cache.backend import InMemoryCacheBackend, StoredEntry
from contextos.cache.engine import SemanticCache
from contextos.embedding.hashing import HashingEmbeddingProvider
from helpers import make_ctx


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t


def _cache(clock: _Clock | None = None) -> SemanticCache:
    now = clock.now if clock is not None else (lambda: datetime(2026, 1, 1, tzinfo=UTC))
    backend = InMemoryCacheBackend(now=now)
    return SemanticCache(backend, HashingEmbeddingProvider(dim=64), now=now)


@pytest.mark.asyncio
async def test_exact_hit() -> None:
    c = _cache()
    ctx = make_ctx("acme", "u1", "alpha")
    await c.store(ctx, "what is kubernetes?", "K8s orchestrates containers", model_id="m1")
    hit = await c.lookup(ctx, "what is kubernetes?", model_id="m1")
    assert hit is not None
    assert hit.tier == "exact"
    assert hit.response_text.startswith("K8s")


@pytest.mark.asyncio
async def test_semantic_hit_on_reordered_query() -> None:
    c = _cache()
    ctx = make_ctx("acme", "u1", "alpha")
    await c.store(ctx, "deploy llm on kubernetes", "use the vllm helm chart", model_id="m1")
    # Same token bag, different order -> exact-tier miss but semantic-tier hit.
    hit = await c.lookup(ctx, "kubernetes deploy llm on", model_id="m1")
    assert hit is not None
    assert hit.tier == "semantic"
    assert hit.similarity >= 0.92


@pytest.mark.asyncio
async def test_no_cross_tenant_hit() -> None:
    c = _cache()
    owner = make_ctx("acme", "u1", "alpha")
    await c.store(owner, "secret question", "secret answer", model_id="m1")
    assert await c.lookup(make_ctx("evil", "x", "alpha"), "secret question", model_id="m1") is None


@pytest.mark.asyncio
async def test_non_cacheable_is_never_stored() -> None:
    c = _cache()
    ctx = make_ctx("acme", "u1", "alpha")
    assert await c.store(ctx, "q", "a", model_id="m1", non_cacheable=True) is False
    assert await c.lookup(ctx, "q", model_id="m1") is None


@pytest.mark.asyncio
async def test_ttl_expiry() -> None:
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    c = _cache(clock)
    ctx = make_ctx("acme", "u1", "alpha")
    await c.store(ctx, "q", "a", model_id="m1", ttl_seconds=10)
    assert await c.lookup(ctx, "q", model_id="m1") is not None       # within TTL
    clock.t = clock.t + timedelta(seconds=11)
    assert await c.lookup(ctx, "q", model_id="m1") is None            # lazily expired


@pytest.mark.asyncio
async def test_model_and_version_partition_the_cache() -> None:
    c = _cache()
    ctx = make_ctx("acme", "u1", "alpha")
    await c.store(ctx, "q", "a", model_id="m1", system_prompt_version="v1")
    assert await c.lookup(ctx, "q", model_id="m2", system_prompt_version="v1") is None  # other model
    assert await c.lookup(ctx, "q", model_id="m1", system_prompt_version="v2") is None  # other sysver


class _BrokenBackend:
    async def get_exact(self, tenant_id: str, key: str) -> StoredEntry | None:
        raise RuntimeError("backend down")

    async def search_semantic(
        self, tenant_id: str, prefilter: str, embedding: list[float], threshold: float
    ) -> tuple[StoredEntry, float] | None:
        raise RuntimeError("backend down")

    async def put(self, stored: StoredEntry) -> None:
        raise RuntimeError("backend down")

    async def health_check(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_fail_open_on_backend_error() -> None:
    c = SemanticCache(_BrokenBackend(), HashingEmbeddingProvider(dim=64))
    ctx = make_ctx("acme", "u1", "alpha")
    assert await c.lookup(ctx, "q", model_id="m1") is None      # miss, never raises
    assert await c.store(ctx, "q", "a", model_id="m1") is False  # no-op, never raises
