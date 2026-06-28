"""A deterministic in-process adapter for tests and offline demos — no network, no GPU."""

from __future__ import annotations

from collections.abc import AsyncIterator

from .base import (
    Capabilities,
    ChatRequest,
    ChatResponse,
    StreamEvent,
    StreamEventType,
    Usage,
)


class FakeAdapter:
    name = "fake"

    def __init__(self, reply: str = "This is a deterministic test reply.") -> None:
        self._reply = reply

    def capabilities(self) -> Capabilities:
        return Capabilities(streaming=True, prefix_cache=True)

    async def health_check(self) -> bool:
        return True

    async def generate(self, req: ChatRequest) -> ChatResponse:
        prompt_tokens = sum(len(m.content.split()) for m in req.messages)
        completion_tokens = len(self._reply.split())
        return ChatResponse(
            text=self._reply,
            model=req.model or "fake-1",
            finish_reason="stop",
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        for tok in self._reply.split():
            yield StreamEvent(type=StreamEventType.TOKEN, data=tok + " ")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
