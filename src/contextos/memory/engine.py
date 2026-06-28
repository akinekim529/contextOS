"""Memory Engine v1 — hybrid retrieval + write.

Pipeline (per docs/design/02-module-deep-dive/2.1-memory-engine.md):
  dense (cosine) + sparse (BM25) -> RRF fuse (k=60) -> recency+importance rescore -> MMR dedup
  -> hard cap <=512 -> return RAW candidates (C1: the Assembler does the final rank/packing).

Isolation: candidate generation goes through the ``StorageBackend`` (tenant+namespace enforced —
the boundary the leakage suite hammers), so the engine inherits zero-cross-tenant-leakage for free
and never has to re-implement scoping. Scope-boundary invariant (ADR-0006): we score over the
pre-retrieved in-scope set and hard-cap at 512 — we never build or own an index.

This reference path scores in-process over the scanned candidates (fine at skeleton scale and the
canonical way to honor "no index"); production swaps the StorageBackend for Postgres, where the
dense leg is pgvector HNSW (18 ms p95) and the sparse leg is Postgres full-text, server-side.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..embedding.base import EmbeddingProvider
from ..embedding.hashing import tokenize
from ..models.common import MemoryTier, Provenance, utcnow
from ..models.memory import MemoryCandidate, MemoryObject
from ..security.context import SecurityContext
from ..store.base import StorageBackend
from .scoring import (
    RRF_K,
    bm25_scores,
    cosine,
    half_life_seconds,
    minmax_normalize,
    recency_factor,
    rrf_fuse,
)

N_DENSE = 200
N_SPARSE = 200
MAX_CANDIDATES = 512          # scope-boundary ceiling — never exceeded into assembly
SCAN_LIMIT = 4096             # in-memory reference: cap rows scanned per retrieve
MMR_LAMBDA = 0.70
COS_DUP = 0.95
W_RRF, W_RECENCY, W_IMPORTANCE = 0.60, 0.25, 0.15   # engine-internal ordering ONLY (C1)


class MemoryEngine:
    def __init__(
        self,
        store: StorageBackend,
        embedder: EmbeddingProvider,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._now = now

    async def write(
        self,
        ctx: SecurityContext,
        content: str,
        *,
        tier: MemoryTier = MemoryTier.SEMANTIC,
        importance: float = 0.5,
        source: str = "chat",
        source_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MemoryObject:
        memory = MemoryObject(
            tenant_id=ctx.tenant_id,
            namespace=ctx.namespace,
            tier=tier,
            content=content,
            importance=importance,
            provenance=Provenance(source=source, source_id=source_id),
            metadata=metadata or {},
        )
        return await self._store.add_memory(ctx, memory)

    async def retrieve(self, ctx: SecurityContext, query: str, *, k: int = 12) -> list[MemoryCandidate]:
        k = min(k, MAX_CANDIDATES)
        rows = await self._store.list_memories(ctx, limit=SCAN_LIMIT)  # scope-enforced (C2)
        if not rows:
            return []

        qvec = await self._embedder.embed(query)
        q_tokens = tokenize(query)
        contents = [r.content for r in rows]
        cvecs = await self._embedder.embed_many(contents)
        doc_tokens = [tokenize(c) for c in contents]

        vector_scores = [cosine(qvec, cv) for cv in cvecs]
        bm25 = bm25_scores(q_tokens, doc_tokens)

        # Dense ranking (cosine desc) and sparse ranking (BM25 desc, positives only).
        dense_idx = sorted(range(len(rows)), key=lambda i: vector_scores[i], reverse=True)[:N_DENSE]
        sparse_idx = [i for i in sorted(range(len(rows)), key=lambda i: bm25[i], reverse=True)
                      if bm25[i] > 0.0][:N_SPARSE]

        id_of = [r.id for r in rows]
        dense_order = [id_of[i] for i in dense_idx]
        sparse_order = [id_of[i] for i in sparse_idx]
        rank_in_dense = {mid: r for r, mid in enumerate(dense_order, start=1)}
        rank_in_sparse = {mid: r for r, mid in enumerate(sparse_order, start=1)}

        rrf = rrf_fuse(dense_order, sparse_order, RRF_K)
        rrf_norm = minmax_normalize(rrf)
        now = self._now()

        vecs: dict[str, list[float]] = {}
        cands: list[MemoryCandidate] = []
        for i, row in enumerate(rows):
            mid = row.id
            if mid not in rrf:        # appeared in neither dense nor sparse list
                continue
            age = max(0.0, (now - row.last_accessed_at).total_seconds())
            vecs[mid] = cvecs[i]
            cands.append(
                MemoryCandidate(
                    memory_id=mid,
                    tenant_id=row.tenant_id,
                    namespace=row.namespace,
                    tier=row.tier,
                    content=row.content,
                    vector_score=vector_scores[i] if mid in rank_in_dense else None,
                    bm25_score=bm25[i] if mid in rank_in_sparse else None,
                    rrf_score=rrf[mid],
                    recency_factor=recency_factor(age, half_life_seconds(row.tier)),
                    importance=row.importance,
                    rank_in_dense=rank_in_dense.get(mid),
                    rank_in_sparse=rank_in_sparse.get(mid),
                    age_seconds=age,
                    source_ref=row.provenance.source_id,
                )
            )

        deduped = self._mmr(cands, rrf_norm, vecs)
        return deduped[:k]

    def _selection_score(self, c: MemoryCandidate, rrf_norm: dict[str, float]) -> float:
        # ENGINE-INTERNAL ordering only. NOT the final rank — the Assembler reranks (C1).
        return (
            W_RRF * rrf_norm.get(c.memory_id, 0.0)
            + W_RECENCY * c.recency_factor
            + W_IMPORTANCE * c.importance
        )

    def _mmr(
        self,
        cands: list[MemoryCandidate],
        rrf_norm: dict[str, float],
        vecs: dict[str, list[float]],
    ) -> list[MemoryCandidate]:
        """Maximal Marginal Relevance: drop near-duplicates (cos >= 0.95) and balance relevance
        against diversity (lambda = 0.70) so the candidate set is diverse before the Assembler."""
        pool = sorted(cands, key=lambda c: self._selection_score(c, rrf_norm), reverse=True)
        selected: list[MemoryCandidate] = []
        while pool and len(selected) < MAX_CANDIDATES:
            best: MemoryCandidate | None = None
            best_val = -1e18
            best_pos = -1
            for pos, c in enumerate(pool):
                max_sim = max((cosine(vecs[c.memory_id], vecs[s.memory_id]) for s in selected), default=0.0)
                if max_sim >= COS_DUP:            # hard duplicate collapse
                    continue
                val = MMR_LAMBDA * self._selection_score(c, rrf_norm) - (1 - MMR_LAMBDA) * max_sim
                if val > best_val:
                    best_val, best, best_pos = val, c, pos
            if best is None:                       # everything left is a duplicate of a selected item
                break
            selected.append(best)
            pool.pop(best_pos)
        return selected
