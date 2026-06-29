"""Context Replay Debugger v1: bit-exact reproduction, content-addressing, C7 modes, isolation."""

from __future__ import annotations

import pytest

from contextos.assembler.budget import TokenBudget
from contextos.assembler.engine import ContextAssembler, ContextSources
from contextos.assembler.tokenizer import HeuristicTokenizer
from contextos.assembler.weights import RankWeights
from contextos.embedding.hashing import HashingEmbeddingProvider
from contextos.models.common import MemoryTier
from contextos.models.memory import MemoryCandidate
from contextos.replay.bundle import BundleMessage, ContextBundle, render_prompt_hash
from contextos.replay.engine import ReplayDebugger, ReplayMode
from contextos.replay.store import InMemoryBundleStore
from contextos.security.context import SecurityContext
from helpers import make_ctx


def _debugger() -> tuple[ReplayDebugger, ContextAssembler]:
    assembler = ContextAssembler(HashingEmbeddingProvider(dim=64))
    return ReplayDebugger(assembler, HeuristicTokenizer(), InMemoryBundleStore()), assembler


def _cands() -> list[MemoryCandidate]:
    return [
        MemoryCandidate(memory_id="m1", tenant_id="acme", namespace="alpha", tier=MemoryTier.SEMANTIC,
                        content="user's prod region is eu-west-1", vector_score=0.9, bm25_score=2.0,
                        rrf_score=0.2, recency_factor=1.0, importance=0.6),
        MemoryCandidate(memory_id="m2", tenant_id="acme", namespace="alpha", tier=MemoryTier.SEMANTIC,
                        content="billing currency is EUR", vector_score=0.4, bm25_score=0.5,
                        rrf_score=0.1, recency_factor=1.0, importance=0.5),
    ]


async def _capture(
    debugger: ReplayDebugger, assembler: ContextAssembler, ctx: SecurityContext
) -> ContextBundle:
    system, user = "You are helpful.", "which region is prod in?"
    cands = _cands()
    budget = TokenBudget(window_tokens=2000, output_reserve=256, system_reserve=10, latest_user_reserve=10)
    weights = RankWeights()
    assembled = await assembler.assemble(
        budget, ContextSources(system, user, cands), weights, HeuristicTokenizer(), mmr_lambda=0.70
    )
    return debugger.capture(
        "trace-1", ctx, system_prompt=system, latest_user_turn=user, candidates=cands,
        weights=weights, mmr_lambda=0.70, budget=budget, model_id="m1",
        rendered_messages=[BundleMessage(role=m.role.value, content=m.content) for m in assembled.messages],
        prompt_hash=render_prompt_hash(assembled.messages),
    )


@pytest.mark.asyncio
async def test_replay_reproduces_prompt_bit_for_bit() -> None:
    debugger, assembler = _debugger()
    ctx = make_ctx("acme", "u1", "alpha")
    bundle = await _capture(debugger, assembler, ctx)

    res = await debugger.replay(ctx, "trace-1")
    assert res is not None
    assert res.prompt_equal is True
    assert res.prompt_hash_actual == res.prompt_hash_expected == bundle.prompt_hash
    assert res.mode is ReplayMode.RECORDED_OUTPUT
    assert res.output_equal is True             # asserted in recorded mode (C7)
    assert res.bundle_cid.startswith("b2:")


@pytest.mark.asyncio
async def test_bundle_is_content_addressed() -> None:
    d1, a = _debugger()
    d2 = ReplayDebugger(a, HeuristicTokenizer(), InMemoryBundleStore())
    ctx = make_ctx("acme", "u1", "alpha")
    b1 = await _capture(d1, a, ctx)
    b2 = await _capture(d2, a, ctx)
    assert b1.bundle_cid == b2.bundle_cid       # identical inputs -> identical CID


@pytest.mark.asyncio
async def test_live_backend_mode_never_asserts_output_equal() -> None:
    debugger, assembler = _debugger()
    ctx = make_ctx("acme", "u1", "alpha")
    await _capture(debugger, assembler, ctx)

    res = await debugger.replay(ctx, "trace-1", mode=ReplayMode.LIVE_BACKEND)
    assert res is not None
    assert res.prompt_equal is True             # deterministic stages still reproduce
    assert res.output_equal is None             # structurally None in live mode (C7)
    assert res.diff is not None


@pytest.mark.asyncio
async def test_attach_output_then_replay_surfaces_completion() -> None:
    debugger, assembler = _debugger()
    ctx = make_ctx("acme", "u1", "alpha")
    await _capture(debugger, assembler, ctx)
    assert debugger.attach_output(ctx, "trace-1", "prod runs in eu-west-1") is True

    res = await debugger.replay(ctx, "trace-1")
    assert res is not None and res.recorded_output == "prod runs in eu-west-1"


@pytest.mark.asyncio
async def test_replay_is_tenant_scoped() -> None:
    debugger, assembler = _debugger()
    await _capture(debugger, assembler, make_ctx("acme", "u1", "alpha"))
    assert await debugger.replay(make_ctx("evil", "x", "alpha"), "trace-1") is None


@pytest.mark.asyncio
async def test_replay_unknown_trace_is_none() -> None:
    debugger, _ = _debugger()
    assert await debugger.replay(make_ctx("acme", "u1", "alpha"), "does-not-exist") is None
