"""Shared primitives for ContextOS data models.

Every persisted object carries a non-null ``tenant_id`` partition key, a ULID id,
and RFC-3339 UTC timestamps. These primitives are the vocabulary the five core
schemas (memory, context block, policy, cache entry, trace span) are built from.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field

# Crockford base32 alphabet (no I, L, O, U) — ULID encoding.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Lexicographically sortable 26-char ULID: 48-bit ms timestamp + 80-bit randomness.

    We roll our own rather than depend on a package so the model layer (and the
    tenant-isolation property tests that hammer it) install with zero extra deps.
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 bits
    value = (ms << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Action(str, Enum):
    """Actions the RBAC firewall arbitrates. ``route`` and ``cache_read`` exist so the
    model router and cache key go through the *same* policy authority (no second store)."""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"
    ROUTE = "route"
    CACHE_READ = "cache_read"


class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class MemoryTier(str, Enum):
    WORKING = "working"          # Redis, TTL — current turn scratch
    SHORT_TERM = "short_term"    # Redis, TTL — recent session
    LONG_TERM = "long_term"      # Postgres+pgvector — durable facts
    EPISODIC = "episodic"        # Postgres+pgvector — project/session episodes
    SEMANTIC = "semantic"        # Postgres+pgvector — consolidated knowledge


class Visibility(str, Enum):
    PRIVATE = "private"          # principal/namespace only
    SHARED_ORG = "shared_org"    # opt-in cross-namespace within a tenant (policy-gated)


class Provenance(BaseModel):
    source: str = Field(..., description="e.g. 'chat', 'ingest', 'consolidation'")
    source_id: str | None = None
    ingested_at: datetime = Field(default_factory=utcnow)


class AccessScope(BaseModel):
    """Who may see a block. ``tenant_id`` + ``namespace`` are *hard* filters (fail-closed)."""

    tenant_id: str
    namespace: str
    visibility: Visibility = Visibility.PRIVATE


class EmbeddingRef(BaseModel):
    """Indirection: the vector lives in the VectorStore; the object holds a reference.

    ``dek_id`` ties the embedding to a per-subject data-encryption key so crypto-shred
    (right-to-be-forgotten) renders the vector inert too — embeddings are in shred scope.
    """

    store: str = Field("pgvector", description="pgvector | qdrant")
    collection: str
    vector_id: str
    dim: int = 384
    dek_id: str | None = None
