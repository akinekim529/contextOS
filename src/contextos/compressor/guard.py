"""Fact-retention guard — the safety net that makes compression trustworthy.

Compression must never *fabricate* a fact. The guard extracts the hard facts (numbers and
alphanumeric IDs like ``eu-west-1`` / ``gpt-4``) from the compressed text and asserts every one
already appears in the original. Extractive compression (a subset) passes trivially; the guard's
real job is catching an abstractive summarizer that invents or mutates a number/ID — in which
case the compressor falls back to a safe, fact-preserving tier.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[\w-]+")


def _facts(text: str) -> set[str]:
    facts: set[str] = set()
    for tok in _TOKEN.findall(text.lower()):
        if any(c.isdigit() for c in tok):
            facts.add(tok)  # pure numbers (12, 1.5) and alphanumeric IDs (eu-west-1, gpt-4)
    return facts


def fact_retention_ok(original: str, compressed: str) -> bool:
    """True when the compressed text introduces no number/ID that is absent from the original."""
    return _facts(compressed) <= _facts(original)
