"""Adapter for any OpenAI-compatible ``/v1/chat/completions`` endpoint.

This single adapter covers vLLM, TGI (with the OpenAI-compatible router), Ollama, and the
OpenAI/Azure APIs — they all speak the same wire format. Backend-specific behaviour
(prefix-cache hint, base URL, auth) is configuration, not a new class.

``httpx`` is imported lazily so importing the package does not require the HTTP stack until a
real call is made.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from .base import (
    Capabilities,
    ChatRequest,
    ChatResponse,
    StreamEvent,
    StreamEventType,
    Usage,
)


class OpenAICompatibleAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        name: str = "openai-compatible",
        prefix_cache: bool = False,
        timeout_s: float = 60.0,
        transport: Any | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._prefix_cache = prefix_cache
        self._timeout_s = timeout_s
        self._transport = transport  # injectable httpx transport (tests / proxies / mTLS)

    def capabilities(self) -> Capabilities:
        return Capabilities(streaming=True, prefix_cache=self._prefix_cache)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _payload(self, req: ChatRequest, *, stream: bool) -> dict[str, Any]:
        return {
            "model": req.model or self._model,
            "messages": [{"role": m.role.value, "content": m.content} for m in req.messages],
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": stream,
        }

    async def health_check(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0, transport=self._transport) as client:
                r = await client.get(f"{self._base_url}/v1/models", headers=self._headers())
                return r.status_code < 500
        except Exception:
            return False

    async def generate(self, req: ChatRequest) -> ChatResponse:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
            r = await client.post(
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                json=self._payload(req, stream=False),
            )
            r.raise_for_status()
            body = r.json()
        choice = body["choices"][0]
        usage = body.get("usage", {})
        return ChatResponse(
            text=choice["message"]["content"],
            model=body.get("model", req.model),
            finish_reason=choice.get("finish_reason"),
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ),
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                json=self._payload(req, stream=True),
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[len("data: "):]
                    if chunk.strip() == "[DONE]":
                        # Server-side terminal event — safe to commit write-back (C8).
                        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
                        return
                    delta = json.loads(chunk)["choices"][0]
                    content = delta.get("delta", {}).get("content")
                    if content:
                        yield StreamEvent(type=StreamEventType.TOKEN, data=content)
