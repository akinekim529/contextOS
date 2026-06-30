"""Async worker plane: runner drain + retry, and memory consolidation."""

from __future__ import annotations

import pytest

from contextos.adapters.fake import FakeAdapter
from contextos.embedding.hashing import HashingEmbeddingProvider
from contextos.memory.engine import MemoryEngine
from contextos.store.memory_store import InMemoryStore
from contextos.workers.consolidation import consolidate_namespace
from contextos.workers.runner import BackgroundRunner
from helpers import make_ctx


@pytest.mark.asyncio
async def test_runner_drains_all_jobs() -> None:
    runner = BackgroundRunner()
    seen: list[int] = []

    async def job() -> None:
        seen.append(1)

    runner.enqueue(job)
    runner.enqueue(job)
    assert runner.pending == 2
    assert await runner.drain() == 2
    assert runner.processed == 2 and len(seen) == 2


@pytest.mark.asyncio
async def test_runner_retries_then_drops() -> None:
    runner = BackgroundRunner(max_retries=1)
    calls = {"n": 0}

    async def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("fail")

    runner.enqueue(boom)
    await runner.drain()
    assert calls["n"] == 2 and runner.failed == 1  # initial + 1 retry, then dropped


@pytest.mark.asyncio
async def test_consolidation_writes_one_durable_memory() -> None:
    eng = MemoryEngine(InMemoryStore(), HashingEmbeddingProvider(dim=64))
    ctx = make_ctx("acme", "u", "alpha")
    await eng.write(ctx, "prod region is eu-west-1")
    await eng.write(ctx, "billing currency is EUR")
    llm = FakeAdapter("consolidated: prod region eu-west-1; billing EUR")

    assert await consolidate_namespace(eng, llm, ctx) == 2
    rows = await eng.all_memories(ctx)
    assert any(r.provenance.source == "consolidation" for r in rows)
