"""Context Compressor v1: structural/extractive/abstractive tiers + fact-retention guard."""

from __future__ import annotations

import pytest

from contextos.adapters.fake import FakeAdapter
from contextos.assembler.tokenizer import HeuristicTokenizer
from contextos.compressor.engine import ContextCompressor
from contextos.compressor.guard import fact_retention_ok

TOK = HeuristicTokenizer()
LONG = (
    "The team plays table tennis on Fridays. "
    "User's prod region is eu-west-1 and the cluster has 12 nodes. "
    "Billing currency is EUR. "
    "The office cafeteria serves coffee. "
    "Marketing prefers blue logos."
)


def test_guard_detects_fabrication() -> None:
    assert fact_retention_ok("region is eu-west-1", "eu-west-1") is True
    assert fact_retention_ok("region is eu-west-1", "region is eu-west-2") is False  # fabricated id
    assert fact_retention_ok("12 nodes", "12 nodes and 99 pods") is False             # fabricated number


@pytest.mark.asyncio
async def test_structural_only_when_under_target() -> None:
    block = await ContextCompressor().compress("hello   world\n\n\n\nbye", 100, TOK)
    assert block.tier in ("none", "structural")
    assert "   " not in block.text          # whitespace collapsed
    assert "\n\n\n" not in block.text        # blank lines collapsed


@pytest.mark.asyncio
async def test_extractive_keeps_query_relevant_sentence() -> None:
    block = await ContextCompressor().compress(
        LONG, target_tokens=12, tokenizer=TOK, query="which region is prod in"
    )
    assert block.tier == "extractive"
    assert "eu-west-1" in block.text                       # the relevant fact survived
    assert block.compressed_tokens < block.original_tokens
    assert block.ratio > 1.0
    assert fact_retention_ok(LONG, block.text)             # no fabrication


@pytest.mark.asyncio
async def test_abstractive_used_when_faithful() -> None:
    llm = FakeAdapter("prod region eu-west-1, 12 nodes")  # a faithful summary
    block = await ContextCompressor().compress(LONG, target_tokens=6, tokenizer=TOK, query="region", llm=llm)
    assert block.tier == "abstractive"
    assert "eu-west-1" in block.text


@pytest.mark.asyncio
async def test_abstractive_fabrication_falls_back_to_extractive() -> None:
    llm = FakeAdapter("prod region eu-west-9 with 999 nodes")  # fabricated facts
    block = await ContextCompressor().compress(LONG, target_tokens=6, tokenizer=TOK, query="region", llm=llm)
    assert block.tier == "extractive"                      # guard rejected the abstractive summary
    assert "eu-west-9" not in block.text and "999" not in block.text
