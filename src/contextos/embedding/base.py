"""EmbeddingProvider abstraction.

ContextOS does not own an embedding model; it calls one behind this interface. The default
production provider is self-hosted ``BAAI/bge-small-en-v1.5`` (384-dim); offline/tests use the
dependency-free :class:`~contextos.embedding.hashing.HashingEmbeddingProvider`. Swapping
providers is a config change, not a code change (design philosophy #1).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    dim: int

    async def embed(self, text: str) -> list[float]: ...

    async def embed_many(self, texts: list[str]) -> list[list[float]]: ...
