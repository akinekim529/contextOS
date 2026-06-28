"""Memory Engine v1: hybrid retrieval, tenant/namespace isolation, MMR dedup, C1 contract."""

from __future__ import annotations

import pytest

from contextos.embedding.hashing import HashingEmbeddingProvider
from contextos.memory.engine import MemoryEngine
from contextos.models.common import MemoryTier
from contextos.store.memory_store import InMemoryStore
from helpers import make_ctx


def _engine() -> MemoryEngine:
    return MemoryEngine(InMemoryStore(), HashingEmbeddingProvider(dim=64))


@pytest.mark.asyncio
async def test_retrieve_finds_relevant_memory_with_raw_scores() -> None:
    eng = _engine()
    ctx = make_ctx("acme", "u1", "alpha")
    await eng.write(ctx, "user's prod region is eu-west-1", tier=MemoryTier.SEMANTIC)
    await eng.write(ctx, "the team enjoys table tennis on fridays")
    await eng.write(ctx, "billing currency is EUR")

    cands = await eng.retrieve(ctx, "which region is production deployed in", k=5)
    assert cands, "expected at least one candidate"
    top = cands[0]
    # The relevant memory should surface near the top.
    assert any("eu-west-1" in c.content for c in cands)
    # C1: raw per-modality signals are present; there is no single blended 'final rank' field.
    assert top.rrf_score > 0.0
    assert 0.0 < top.recency_factor <= 1.0
    assert (top.vector_score is not None) or (top.bm25_score is not None)
    assert not hasattr(top, "final_score")


@pytest.mark.asyncio
async def test_retrieve_is_tenant_and_namespace_scoped() -> None:
    eng = _engine()
    owner = make_ctx("acme", "u1", "alpha")
    await eng.write(owner, "secret: acme prod region is eu-west-1")

    # Different tenant — must see nothing.
    assert await eng.retrieve(make_ctx("evil", "x", "alpha"), "region", k=5) == []
    # Same tenant, different namespace — must see nothing (C2 hard filter).
    assert await eng.retrieve(make_ctx("acme", "u2", "beta"), "region", k=5) == []
    # Owner sees it.
    assert await eng.retrieve(owner, "region", k=5)


@pytest.mark.asyncio
async def test_mmr_collapses_near_duplicates() -> None:
    eng = _engine()
    ctx = make_ctx("acme", "u1", "alpha")
    # Two identical memories (cos == 1.0 >= 0.95 dup threshold) + one unrelated.
    await eng.write(ctx, "the sky is blue today")
    await eng.write(ctx, "the sky is blue today")
    await eng.write(ctx, "kubernetes schedules pods onto nodes")

    cands = await eng.retrieve(ctx, "what color is the sky", k=10)
    dup_hits = [c for c in cands if c.content == "the sky is blue today"]
    assert len(dup_hits) == 1, "MMR should collapse the exact-duplicate memory"


@pytest.mark.asyncio
async def test_retrieve_empty_scope_returns_empty() -> None:
    eng = _engine()
    assert await eng.retrieve(make_ctx("acme", "u1", "alpha"), "anything", k=5) == []
