"""Context Assembler v1: injection, inviolable hard reserves, 413, edge-loading, weights, C1."""

from __future__ import annotations

import pytest

from contextos.adapters.base import Role
from contextos.assembler.budget import ContextOverflow, TokenBudget
from contextos.assembler.engine import ContextAssembler, ContextSources
from contextos.assembler.tokenizer import HeuristicTokenizer
from contextos.assembler.weights import PolicyError, RankWeights
from contextos.embedding.hashing import HashingEmbeddingProvider
from contextos.models.common import MemoryTier
from contextos.models.memory import MemoryCandidate

TOK = HeuristicTokenizer()


def _asm() -> ContextAssembler:
    return ContextAssembler(HashingEmbeddingProvider(dim=64))


def _cand(mid: str, content: str, *, vec: float = 0.5, bm25: float = 1.0,
          rec: float = 1.0, imp: float = 0.5) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=mid, tenant_id="acme", namespace="alpha", tier=MemoryTier.SEMANTIC,
        content=content, vector_score=vec, bm25_score=bm25, rrf_score=0.1,
        recency_factor=rec, importance=imp,
    )


def test_weights_must_sum_to_one() -> None:
    RankWeights().validate()
    with pytest.raises(PolicyError):
        RankWeights(vector=0.9).validate()


@pytest.mark.asyncio
async def test_assemble_injects_memory_and_brackets_prompt() -> None:
    sources = ContextSources(
        system_prompt="You are helpful.",
        latest_user_turn="which region is prod in?",
        candidates=[_cand("m1", "user's prod region is eu-west-1", vec=0.9, bm25=2.0)],
    )
    budget = TokenBudget(window_tokens=2000, output_reserve=256, system_reserve=10, latest_user_reserve=10)
    out = await _asm().assemble(budget, sources, RankWeights(), TOK)

    assert "eu-west-1" in out.render()                     # memory reached the prompt
    assert out.messages[0].role == Role.SYSTEM             # system always first
    assert out.messages[-1].role == Role.USER              # latest user turn always last
    assert out.messages[-1].content == "which region is prod in?"
    assert any(d.kept for d in out.decisions)


@pytest.mark.asyncio
async def test_hard_reserves_inviolable_under_budget_pressure() -> None:
    cands = [
        _cand(f"m{i}", f"fact {i} about kubernetes pods and nodes and scheduling " * 4)
        for i in range(40)
    ]
    sources = ContextSources(system_prompt="SYS", latest_user_turn="the user question", candidates=cands)
    budget = TokenBudget(
        window_tokens=320, output_reserve=100,
        system_reserve=TOK.count("SYS"), latest_user_reserve=TOK.count("the user question"),
    )
    out = await _asm().assemble(budget, sources, RankWeights(), TOK)

    assert out.messages[0].content == "SYS"                # never evicted
    assert out.messages[-1].content == "the user question"  # never evicted
    injected = out.used_tokens - TOK.count("SYS") - TOK.count("the user question")
    assert injected <= budget.soft_budget                  # soft pool respected
    assert any(not d.kept for d in out.decisions)          # some candidates evicted


@pytest.mark.asyncio
async def test_assemble_fails_closed_on_overflow() -> None:
    budget = TokenBudget(window_tokens=50, output_reserve=40, system_reserve=20, latest_user_reserve=20)
    with pytest.raises(ContextOverflow):
        await _asm().assemble(
            budget, ContextSources("s", "u", []), RankWeights(), TOK
        )


@pytest.mark.asyncio
async def test_edge_loading_places_strongest_at_an_edge() -> None:
    sources = ContextSources(
        system_prompt="S",
        latest_user_turn="Q",
        candidates=[
            _cand("mid", "beta middling fact", vec=0.5, bm25=1.0, imp=0.5),
            _cand("weak", "gamma weak fact", vec=0.1, bm25=0.2, imp=0.1),
            _cand("strong", "alpha unique salient fact", vec=0.99, bm25=3.0, imp=1.0),
        ],
    )
    budget = TokenBudget(window_tokens=2000, output_reserve=100,
                         system_reserve=TOK.count("S"), latest_user_reserve=TOK.count("Q"))
    out = await _asm().assemble(budget, sources, RankWeights(), TOK)

    kept = [d for d in out.decisions if d.kept]
    n = len(kept)
    strong = next(d for d in out.decisions if d.memory_id == "strong")
    weak = next(d for d in out.decisions if d.memory_id == "weak")
    assert strong.blended > weak.blended          # C1: the assembler computed the blend
    assert strong.placement in (0, n - 1)          # strongest sits on an edge
