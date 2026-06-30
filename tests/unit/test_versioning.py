"""Git-like memory versioning: commit, diff, rollback, branch, tenant isolation."""

from __future__ import annotations

import pytest

from contextos.embedding.hashing import HashingEmbeddingProvider
from contextos.memory.engine import MemoryEngine
from contextos.store.memory_store import InMemoryStore
from contextos.versioning.engine import MemoryVersioning, UnknownCommit
from helpers import make_ctx


def _engine() -> MemoryEngine:
    return MemoryEngine(InMemoryStore(), HashingEmbeddingProvider(dim=64))


@pytest.mark.asyncio
async def test_commit_and_diff() -> None:
    eng = _engine()
    v = MemoryVersioning(eng)
    ctx = make_ctx("acme", "u", "alpha")
    await eng.write(ctx, "fact one")
    c1 = await v.commit(ctx, "first")
    await eng.write(ctx, "fact two")
    c2 = await v.commit(ctx, "second")

    d = v.diff(ctx, c1, c2)
    assert len(d.added) == 1 and not d.removed and not d.changed
    assert "main" in v.branches(ctx)


@pytest.mark.asyncio
async def test_rollback_restores_lost_memories() -> None:
    eng = _engine()
    v = MemoryVersioning(eng)
    ctx = make_ctx("acme", "u", "alpha")
    await eng.write(ctx, "important fact eu-west-1")
    cid = await v.commit(ctx)

    # Simulate the backend losing the data: point versioning at a fresh, empty engine.
    v._engine = _engine()
    assert await v.rollback(ctx, cid) == 1
    rows = await v._engine.all_memories(ctx)
    assert any("eu-west-1" in r.content for r in rows)


@pytest.mark.asyncio
async def test_versioning_is_tenant_scoped() -> None:
    eng = _engine()
    v = MemoryVersioning(eng)
    owner = make_ctx("acme", "u", "alpha")
    await eng.write(owner, "secret")
    cid = await v.commit(owner)

    intruder = make_ctx("evil", "x", "alpha")
    assert v.get(intruder, cid) is None
    with pytest.raises(UnknownCommit):
        v.diff(intruder, cid, cid)
