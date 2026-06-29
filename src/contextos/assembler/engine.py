"""Context Assembler v1 — the sole final-ranking + budget authority (C1).

Per docs/design/02-module-deep-dive/2.2-context-assembler.md:
  blend raw signals into ONE relevance scalar -> MMR diversity (lambda=0.70 over top-128)
  -> greedy knapsack into the soft budget -> edge-load placement (fight lost-in-the-middle)
  -> render the final prompt messages. Pure CPU, stateless; fails closed (413) on hard-reserve
  overflow. The system prompt is always first, the latest user turn always last.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..adapters.base import ChatMessage, Role
from ..embedding.base import EmbeddingProvider
from ..memory.scoring import cosine
from ..models.memory import MemoryCandidate
from .budget import TokenBudget, check_hard_reserves
from .tokenizer import Tokenizer, conservative_count
from .weights import RankWeights

MMR_TOPK = 128  # MMR runs over the top-K by blended score; beyond this diversity gain is noise


@dataclass(frozen=True)
class Turn:
    role: Role
    content: str


@dataclass(frozen=True)
class ContextSources:
    system_prompt: str
    latest_user_turn: str
    candidates: list[MemoryCandidate]   # <=512, already ACL-filtered + compressed
    history: list[Turn] = field(default_factory=list)


@dataclass
class CandidateDecision:
    memory_id: str
    blended: float
    kept: bool
    reason: str
    placement: int | None = None  # position in the final context ordering; None if evicted


@dataclass(frozen=True)
class AssembledContext:
    messages: list[ChatMessage]
    used_tokens: int
    budget: TokenBudget
    decisions: list[CandidateDecision]

    def render(self) -> str:
        return "\n\n".join(f"{m.role.value}: {m.content}" for m in self.messages)


def _source_prior(c: MemoryCandidate) -> float:
    # v1 neutral prior; pinned memories (importance == 1.0) carry full provenance trust.
    return 1.0 if c.importance >= 1.0 else 0.5


class ContextAssembler:
    def __init__(self, embedder: EmbeddingProvider) -> None:
        self._embedder = embedder

    async def assemble(
        self,
        budget: TokenBudget,
        sources: ContextSources,
        weights: RankWeights,
        tokenizer: Tokenizer | None,
        *,
        mmr_lambda: float = 0.70,
    ) -> AssembledContext:
        weights.validate()
        check_hard_reserves(budget)  # fail-closed (413) before any packing work

        def count(text: str) -> int:
            return tokenizer.count(text) if tokenizer is not None else conservative_count(None, text)

        scored = self._blend(sources.candidates, weights)
        selected = await self._mmr(scored, mmr_lambda)
        survivors, decisions = self._knapsack(scored, selected, budget.soft_budget, count)
        placed = _edge_load(survivors)

        pos_by_id = {c.memory_id: i for i, (c, _) in enumerate(placed)}
        for d in decisions:
            if d.kept:
                d.placement = pos_by_id.get(d.memory_id)

        messages = _render(sources, placed)
        used = (
            count(sources.system_prompt)
            + count(sources.latest_user_turn)
            + sum(count(c.content) for c, _ in placed)
            + sum(count(t.content) for t in sources.history)
        )
        return AssembledContext(messages=messages, used_tokens=used, budget=budget, decisions=decisions)

    def _blend(
        self, candidates: list[MemoryCandidate], w: RankWeights
    ) -> list[tuple[MemoryCandidate, float]]:
        # max-normalize BM25 across the candidate set (0 when there is no sparse signal).
        max_bm = max((c.bm25_score or 0.0 for c in candidates), default=0.0)
        scored: list[tuple[MemoryCandidate, float]] = []
        for c in candidates:
            vec = c.vector_score or 0.0
            lex = (c.bm25_score or 0.0) / max_bm if max_bm > 0.0 else 0.0
            blended = (
                w.vector * vec
                + w.lexical * lex
                + w.recency * c.recency_factor
                + w.importance * c.importance
                + w.source * _source_prior(c)
            )
            scored.append((c, blended))
        return scored

    async def _mmr(
        self, scored: list[tuple[MemoryCandidate, float]], lam: float
    ) -> list[tuple[MemoryCandidate, float]]:
        ranked = sorted(scored, key=lambda cb: cb[1], reverse=True)[:MMR_TOPK]
        if not ranked:
            return []
        vecs = await self._embedder.embed_many([c.content for c, _ in ranked])
        vec_by_id = {ranked[i][0].memory_id: vecs[i] for i in range(len(ranked))}

        pool = list(ranked)
        selected: list[tuple[MemoryCandidate, float]] = []
        max_sim: dict[str, float] = {c.memory_id: 0.0 for c, _ in ranked}
        while pool:
            best_pos = max(
                range(len(pool)),
                key=lambda i: lam * pool[i][1] - (1 - lam) * max_sim[pool[i][0].memory_id],
            )
            chosen, chosen_blended = pool.pop(best_pos)
            selected.append((chosen, chosen_blended))
            cvec = vec_by_id[chosen.memory_id]
            for c, _ in pool:  # incremental max-sim update keeps MMR at O(K^2)
                sim = cosine(vec_by_id[c.memory_id], cvec)
                if sim > max_sim[c.memory_id]:
                    max_sim[c.memory_id] = sim
        return selected

    def _knapsack(
        self,
        scored: list[tuple[MemoryCandidate, float]],
        selected: list[tuple[MemoryCandidate, float]],
        soft_budget: int,
        count: Callable[[str], int],
    ) -> tuple[list[tuple[MemoryCandidate, float]], list[CandidateDecision]]:
        survivors: list[tuple[MemoryCandidate, float]] = []
        decisions: list[CandidateDecision] = []
        used = 0
        for c, blended in selected:  # MMR order: most marginally-relevant first
            toks = count(c.content)
            if used + toks <= soft_budget:
                used += toks
                survivors.append((c, blended))
                decisions.append(CandidateDecision(c.memory_id, blended, True, "packed"))
            else:
                decisions.append(CandidateDecision(c.memory_id, blended, False, "evicted: soft budget full"))
        selected_ids = {c.memory_id for c, _ in selected}
        for c, blended in scored:
            if c.memory_id not in selected_ids:
                decisions.append(CandidateDecision(c.memory_id, blended, False, "evicted: below MMR top-K"))
        return survivors, decisions


def _edge_load(survivors: list[tuple[MemoryCandidate, float]]) -> list[tuple[MemoryCandidate, float]]:
    """Strongest survivors at both edges, weakest buried in the middle (attention geometry).

    Process weakest-first and alternate ends, so the strongest items (processed last) land on
    the outer edges where the model attends most reliably. Deterministic tie-break on id.
    """
    ranked = sorted(survivors, key=lambda cb: (cb[1], cb[0].memory_id))  # ascending = weakest first
    out: deque[tuple[MemoryCandidate, float]] = deque()
    for i, item in enumerate(ranked):
        if i % 2 == 0:
            out.append(item)
        else:
            out.appendleft(item)
    return list(out)


def _render(sources: ContextSources, placed: list[tuple[MemoryCandidate, float]]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    if sources.system_prompt:
        messages.append(ChatMessage(role=Role.SYSTEM, content=sources.system_prompt))
    if placed:
        body = "\n".join(f"- {c.content}" for c, _ in placed)
        messages.append(ChatMessage(role=Role.SYSTEM, content=f"Relevant context:\n{body}"))
    for t in sources.history:
        messages.append(ChatMessage(role=t.role, content=t.content))
    messages.append(ChatMessage(role=Role.USER, content=sources.latest_user_turn))
    return messages
