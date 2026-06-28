"""Dependency-free deterministic embeddings via feature hashing.

This exists so the Memory Engine (and its tests) run with **zero ML dependencies** — no
torch, no model download, no GPU. It is a real cosine signal: tokens are hashed into a
fixed-dim vector (signed feature hashing, the "hashing trick") and L2-normalized, so
similar texts share dimensions and score high. It is NOT a semantic model — the production
path uses ``BAAI/bge-small-en-v1.5`` behind the same :class:`EmbeddingProvider` interface.

We use BLAKE2b (not Python's builtin ``hash``) because ``hash`` is salted per process and
would make embeddings non-deterministic across runs — fatal for replay and for stable tests.
"""

from __future__ import annotations

import hashlib
import math
import re

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class HashingEmbeddingProvider:
    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _hash(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokenize(text):
            digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            h = int.from_bytes(digest, "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    async def embed(self, text: str) -> list[float]:
        return self._hash(text)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self._hash(t) for t in texts]
