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
    """A retrieval hit with raw, un-blended signals. The assembler blends + ranks these."""

    memory_id: str
    tenant_id: str
    namespace: str
    content: str
    tier: MemoryTier
    importance: float = 0.5
    # raw per-modality scores — NOT a final rank (C1)
    vector_score: float | None = None      # cosine similarity, pgvector
    lexical_score: float | None = None      # BM25 / ts_rank
    recency_score: float | None = None      # decay over last_accessed_at
    raw_scores: dict[str, float] = Field(default_factory=dict)
