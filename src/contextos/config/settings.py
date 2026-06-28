"""Layered configuration: code defaults <- environment (CONTEXTOS_*) <- .env / Secret.

Pydantic Settings gives typed, validated config so a misconfigured backend URL or a missing
DSN fails at startup, not mid-request. Stores are optional at the walking-skeleton stage —
absent a DSN, the gateway uses the in-memory store so ``hello world`` needs no infrastructure.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class ContextOSSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTEXTOS_", env_file=".env", extra="ignore")

    # Backend (forwarded to — never run by — ContextOS)
    backend_base_url: str = "http://localhost:8000"
    backend_api_key: str | None = None
    default_model: str = "meta-llama/Llama-3.1-8B-Instruct"
    backend_kind: str = "vllm"  # vllm | openai | ollama | tgi | fake

    # Stores (optional at skeleton stage)
    postgres_dsn: str | None = None
    redis_url: str | None = None

    # Budgets / behaviour
    default_token_budget: int = 6000
    max_candidates: int = 512  # scope-boundary hard cap (never build an index)

    # Service
    log_level: str = "INFO"
