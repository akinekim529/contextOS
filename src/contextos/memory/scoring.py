"""Pure scoring functions for hybrid retrieval. No I/O, no state — trivially unit-testable.

These implement the canonical algorithm from the design (docs/design/02-module-deep-dive/
2.1-memory-engine.md): dense+sparse fused by Reciprocal Rank Fusion (k=60), recency via
per-tier exponential half-life decay, and a BM25 sparse leg. The Memory Engine composes them;
it never collapses them into one downstream rank (C1) — the Assembler does that.
"""

from __future__ import annotations

import math
from collections import Counter

from ..models.common import MemoryTier

RRF_K = 60  # canonical RRF constant (Cormack et al.)

# Per-tier recency half-lives in seconds (working 30m, short 12h, episodic 7d, semantic 90d).
_HALF_LIFE_SECONDS: dict[MemoryTier, float] = {
    MemoryTier.WORKING: 30 * 60,
    MemoryTier.SHORT_TERM: 12 * 3600,
    MemoryTier.LONG_TERM: 30 * 86400,
    MemoryTier.EPISODIC: 7 * 86400,
    MemoryTier.SEMANTIC: 90 * 86400,
}


def half_life_seconds(tier: MemoryTier) -> float:
    return _HALF_LIFE_SECONDS[tier]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Inputs may already be L2-normalized; we normalize defensively."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def recency_factor(age_seconds: float, half_life: float) -> float:
    """``0.5 ** (age / half_life)`` — 1.0 at age 0, 0.5 at one half-life, decaying in (0, 1]."""
    if half_life <= 0:
        return 1.0
    return float(0.5 ** (max(0.0, age_seconds) / half_life))


def rrf_fuse(dense_order: list[str], sparse_order: list[str], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(id) = sum 1/(k + rank) over the lists it appears in.

    Rank-based (not score-based) on purpose: dense cosine and BM25 are on incomparable scales,
    so fusing by position is robust where a weighted sum of raw scores is brittle.
    """
    scores: dict[str, float] = {}
    for order in (dense_order, sparse_order):
        for rank, mid in enumerate(order, start=1):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
    return scores


def minmax_normalize(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi == lo:
        return {key: 1.0 for key in values}
    span = hi - lo
    return {key: (v - lo) / span for key, v in values.items()}


def bm25_scores(
    query_tokens: list[str],
    docs_tokens: list[list[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """BM25 over the candidate set (idf computed from the candidates themselves).

    Mirrors the role of Postgres ``ts_rank_cd`` on the in-memory reference path; in production
    the sparse leg is Postgres full-text search server-side.
    """
    n = len(docs_tokens)
    if n == 0:
        return []
    doc_lens = [len(d) for d in docs_tokens]
    avgdl = sum(doc_lens) / n if n else 0.0
    # document frequency per query term
    df: Counter[str] = Counter()
    q_unique = set(query_tokens)
    for d in docs_tokens:
        seen = set(d) & q_unique
        for term in seen:
            df[term] += 1
    idf = {
        term: math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
        for term in q_unique
    }
    out: list[float] = []
    for d, dl in zip(docs_tokens, doc_lens, strict=True):
        tf = Counter(d)
        score = 0.0
        for term in q_unique:
            f = tf.get(term, 0)
            if f == 0:
                continue
            denom = f + k1 * (1 - b + b * (dl / avgdl if avgdl else 0.0))
            score += idf[term] * (f * (k1 + 1)) / denom
        out.append(score)
    return out
