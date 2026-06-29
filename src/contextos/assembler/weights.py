"""The single relevance weight vocabulary (C1).

The Memory Engine hands the Assembler raw per-modality signals; this is the *only* place in
the system that fuses them into one comparable scalar. Weights live on the tenant policy;
these are the defaults. They must sum to 1.0 or the policy fails to load (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass


class PolicyError(ValueError):
    """A policy is malformed — surfaced at load time, never silently coerced."""


@dataclass(frozen=True)
class RankWeights:
    vector: float = 0.40      # dense semantic similarity (cosine)
    lexical: float = 0.20     # BM25 / sparse exact-term overlap
    recency: float = 0.15     # memory-decay freshness (orthogonal to placement)
    importance: float = 0.15  # write-time / consolidation-assigned salience
    source: float = 0.10      # provenance trust prior (pinned > curated > scraped)

    def validate(self) -> None:
        total = self.vector + self.lexical + self.recency + self.importance + self.source
        if abs(total - 1.0) > 1e-6:
            raise PolicyError(f"RankWeights must sum to 1.0, got {total:.6f}")
