"""Semantic cache entry. Keys are tenant-salted; sharing is per-tenant only (no leaks).

Fingerprint policy (ADR-0004 / C6): the coarse signature is
``hash(query_embedding_bucket + model_id + system_prompt_version + stable_fact_set_version)``.
Responses grounded in a principal's private memory set ``non_cacheable=True`` and are
never written — that honesty is why the realistic hit-ratio target is 25-45%, not higher.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .common import utcnow


class CacheEntry(BaseModel):
    key: str = Field(..., description="tenant-salted exact-hash key")
    tenant_id: str
    fingerprint: str = Field(..., description="coarse semantic signature for the ANN tier")
    response_ref: str = Field(..., description="pointer to the stored response body (Redis)")
    model_id: str
    ttl_seconds: int = 3600
    created_at: datetime = Field(default_factory=utcnow)
    hit_count: int = 0
    non_cacheable: bool = Field(False, description="private-memory-grounded -> never served from cache")
