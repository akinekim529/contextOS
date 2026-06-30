"""TGI (Text Generation Inference) adapter — TGI's OpenAI-compatible Messages API speaks the
same wire format, so it is configuration over :class:`OpenAICompatibleAdapter`."""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleAdapter


def tgi_adapter(*, base_url: str, model: str, api_key: str | None = None) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(base_url=base_url, model=model, api_key=api_key, name="tgi")
