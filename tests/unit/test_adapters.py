"""Adapter factories + OpenAI-compatible wire format (driven through httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from contextos.adapters.base import ChatMessage, ChatRequest, Role, StreamEventType
from contextos.adapters.ollama import ollama_adapter
from contextos.adapters.openai_compatible import OpenAICompatibleAdapter
from contextos.adapters.tgi import tgi_adapter
from contextos.adapters.vllm import vllm_adapter


def test_factories_set_names_and_capabilities() -> None:
    assert vllm_adapter(base_url="http://x", model="m").name == "vllm"
    assert vllm_adapter(base_url="http://x", model="m").capabilities().prefix_cache is True
    assert ollama_adapter(base_url="http://x", model="m").name == "ollama"
    assert tgi_adapter(base_url="http://x", model="m").name == "tgi"


def _adapter(handler: object) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        base_url="http://x", model="m", transport=httpx.MockTransport(handler)  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_generate_speaks_openai_wire_format() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["messages"][0]["content"] == "hi"
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    resp = await _adapter(handler).generate(
        ChatRequest(model="m", messages=[ChatMessage(role=Role.USER, content="hi")])
    )
    assert resp.text == "hello"
    assert resp.finish_reason == "stop"
    assert resp.usage.total_tokens == 4


@pytest.mark.asyncio
async def test_stream_parses_sse_tokens() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    adapter = _adapter(handler)
    events = [
        e async for e in adapter.stream(
            ChatRequest(model="m", messages=[ChatMessage(role=Role.USER, content="hi")])
        )
    ]
    text = "".join(e.data for e in events if e.type is StreamEventType.TOKEN)
    assert text == "Hello"
    assert events[-1].type is StreamEventType.DONE
