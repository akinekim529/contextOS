"""Unit tests for the pure retrieval scoring functions."""

from __future__ import annotations

import math

from contextos.memory.scoring import (
    bm25_scores,
    cosine,
    half_life_seconds,
    minmax_normalize,
    recency_factor,
    rrf_fuse,
)
from contextos.models.common import MemoryTier


def test_cosine_bounds() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector -> 0, no div-by-zero


def test_recency_decay_half_life() -> None:
    assert recency_factor(0, 100) == 1.0
    assert recency_factor(100, 100) == 0.5
    assert math.isclose(recency_factor(200, 100), 0.25)


def test_half_life_per_tier() -> None:
    assert half_life_seconds(MemoryTier.EPISODIC) == 7 * 86400
    assert half_life_seconds(MemoryTier.SEMANTIC) == 90 * 86400
    assert half_life_seconds(MemoryTier.SEMANTIC) > half_life_seconds(MemoryTier.EPISODIC)


def test_rrf_rewards_top_ranks_in_both_lists() -> None:
    fused = rrf_fuse(dense_order=["a", "b", "c"], sparse_order=["a", "c", "b"], k=60)
    # 'a' is #1 in both lists -> strictly highest fused score.
    assert fused["a"] > fused["b"]
    assert fused["a"] > fused["c"]


def test_minmax_normalize_range() -> None:
    out = minmax_normalize({"x": 10.0, "y": 20.0, "z": 30.0})
    assert out["x"] == 0.0 and out["z"] == 1.0
    assert minmax_normalize({"only": 5.0}) == {"only": 1.0}  # degenerate -> 1.0


def test_bm25_prefers_docs_with_query_terms() -> None:
    docs = [
        ["deploy", "llm", "kubernetes", "helm"],
        ["bake", "a", "chocolate", "cake"],
        ["llm", "kubernetes", "vllm"],
    ]
    scores = bm25_scores(["llm", "kubernetes"], docs)
    assert scores[0] > scores[1]   # has the terms vs none
    assert scores[2] > scores[1]
