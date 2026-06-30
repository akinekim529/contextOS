"""Context Compressor v1 — three real tiers, fail-safe.

structural (whitespace/boilerplate) → extractive (keep the query-relevant sentences) →
abstractive (a real summarization call through a backend adapter, only when one is supplied).
Each tier returns a real result; the next tier runs only if the previous is still over budget.
The fact-retention guard gates every lossy tier: if a tier would drop or fabricate a fact, the
compressor falls back to the last fact-preserving result (fail-safe), never silently lying.

Compression runs AFTER ACL/redaction (pipeline invariant) and never builds an index — it scores
over the sentences of one already-retrieved block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..adapters.base import BackendAdapter, ChatMessage, ChatRequest, Role
from ..assembler.tokenizer import Tokenizer
from .guard import fact_retention_ok

_WS = re.compile(r"[ \t]+")
_BLANK = re.compile(r"\n{3,}")
_SENT = re.compile(r"[^.!?]+[.!?]?")
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class CompressedBlock:
    text: str
    original_tokens: int
    compressed_tokens: int
    tier: str            # none | structural | extractive | abstractive
    guard_passed: bool

    @property
    def ratio(self) -> float:
        return self.original_tokens / self.compressed_tokens if self.compressed_tokens else 1.0


def _structural(text: str) -> str:
    return _BLANK.sub("\n\n", _WS.sub(" ", text)).strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.findall(text) if s.strip()]


class ContextCompressor:
    async def compress(
        self,
        text: str,
        target_tokens: int,
        tokenizer: Tokenizer,
        *,
        query: str | None = None,
        llm: BackendAdapter | None = None,
    ) -> CompressedBlock:
        original_tokens = tokenizer.count(text)
        structural = _structural(text)
        s_tok = tokenizer.count(structural)
        if s_tok <= target_tokens:
            tier = "structural" if structural != text else "none"
            return CompressedBlock(structural, original_tokens, s_tok, tier, True)

        extractive = self._extractive(structural, target_tokens, tokenizer, query)
        if not fact_retention_ok(text, extractive):
            # Extractive somehow introduced a fact mismatch — keep structural (fail-safe).
            return CompressedBlock(structural, original_tokens, s_tok, "structural", False)
        e_tok = tokenizer.count(extractive)

        if llm is not None and e_tok > target_tokens:
            abstractive = await self._abstractive(structural, target_tokens, llm)
            if abstractive and fact_retention_ok(text, abstractive):
                a_tok = tokenizer.count(abstractive)
                if a_tok <= e_tok:
                    return CompressedBlock(abstractive, original_tokens, a_tok, "abstractive", True)
            # abstractive fabricated/grew or failed -> fall back to extractive (fail-safe)

        return CompressedBlock(extractive, original_tokens, e_tok, "extractive", True)

    def _extractive(self, text: str, target_tokens: int, tokenizer: Tokenizer, query: str | None) -> str:
        sentences = _sentences(text)
        if not sentences:
            return text
        q = set(_WORD.findall(query.lower())) if query else set()
        scored: list[tuple[int, str, float]] = []
        for i, s in enumerate(sentences):
            toks = set(_WORD.findall(s.lower()))
            overlap = float(len(toks & q)) if q else 0.0
            scored.append((i, s, overlap + 0.1 / (i + 1)))  # query overlap, slight lead-bias

        kept: set[int] = set()
        used = 0
        for i, s, _ in sorted(scored, key=lambda x: x[2], reverse=True):
            t = tokenizer.count(s)
            if used + t > target_tokens and kept:
                continue
            kept.add(i)
            used += t
            if used >= target_tokens:
                break
        return " ".join(s for i, s, _ in scored if i in kept)

    async def _abstractive(self, text: str, target_tokens: int, llm: BackendAdapter) -> str | None:
        prompt = (
            f"Summarize the context below in at most {max(1, target_tokens)} tokens. Preserve every "
            f"number, identifier, and named entity verbatim; do not invent facts.\n\n{text}"
        )
        try:
            resp = await llm.generate(
                ChatRequest(
                    model="compressor",
                    messages=[ChatMessage(role=Role.USER, content=prompt)],
                    max_tokens=max(64, target_tokens * 2),
                )
            )
            return resp.text.strip()
        except Exception:
            return None  # fail-safe: caller keeps the extractive result
