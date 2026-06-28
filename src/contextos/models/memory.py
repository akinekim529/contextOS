"""Memory objects and retrieval candidates.

Boundary (ADR-0005 / C1): the Memory Engine returns ``MemoryCandidate`` rows carrying
*raw per-modality scores only*. It never produces a final ranking or packs a budget —
that is the Context Assembler's sole job. Keeping raw scores here means the assembler
owns one weight vocabulary instead of two diverging ones.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .common import EmbeddingRef, MemoryTier, Provenance, new_ulid, utcnow


class MemoryObject(BaseModel):
    id: str = Field(default_factory=new_ulid)
    tenant_id: str
    namespace: str = Field(..., description="project/agent/user partition WITHIN a tenant; hard filter")
    tier: MemoryTier
    content: str
    embedding_ref: EmbeddingRef | None = None
    importance: float = Field(0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utcnow)
    last_accessed_at: datetime = Field(default_factory=utcnow)
    provenance: Provenance
    metadata: dict[str, str] = Field(default_factory=dict)


class MemoryCandidate(BaseModel):
    """A retrieval hit carrying RAW per-modality signals only. The Context Assembler (s2.2)
    computes the single final rank and does budget packing; the Memory Engine never blends
    these into one downstream-ranking number (C1). ``rrf_score`` is an internal ordering
    signal, reported for transparency/replay, not the final rank."""

    memory_id: str
    tenant_id: str
    namespace: str
    tier: MemoryTier
    content: str  # raw, pre-compression (compression runs AFTER ACL/redaction)
    # --- raw per-modality signals (un-blended) ---
    vector_score: float | None = None   # cosine in [0,1]; None if sparse-only hit
    bm25_score: float | None = None      # lexical rank; None if dense-only hit
    rrf_score: float = 0.0               # fusion rank score (internal ordering only)
    recency_factor: float = 1.0          # exp half-life decay in (0,1]
    importance: float = 0.5              # [0,1]
    rank_in_dense: int | None = None     # 1-based; None if not in dense list
    rank_in_sparse: int | None = None    # 1-based; None if not in sparse list
    age_seconds: float = 0.0
    source_ref: str | None = None        # READ-ONLY correlation
