"""vLLM adapter — an OpenAI-compatible endpoint with prefix caching enabled.

vLLM exposes an OpenAI-compatible server, so there is no separate wire implementation. The
only meaningful difference is ``prefix_cache=True``: ContextOS constructs prompts with a
stable prefix (system + long-lived memory first, volatile turn last) so vLLM's automatic
prefix KV-cache reuse kicks in and time-to-first-token drops on repeated prefixes.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleAdapter


def vllm_adapter(*, base_url: str, model: str, api_key: str | None = None) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        base_url=base_url,
        model=model,
        api_key=api_key,
        name="vllm",
        prefix_cache=True,
    )
