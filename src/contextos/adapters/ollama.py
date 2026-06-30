"""Ollama adapter — Ollama serves an OpenAI-compatible ``/v1`` endpoint, so it is configuration
over :class:`OpenAICompatibleAdapter`, not a new wire implementation."""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleAdapter


def ollama_adapter(*, base_url: str, model: str, api_key: str | None = None) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(base_url=base_url, model=model, api_key=api_key, name="ollama")
