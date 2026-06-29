"""The context bundle — the frozen, self-describing record of one request's context journey.

It is **content-addressed**: its identity (`bundle_cid`) is a hash of its canonical inputs, so
two requests that assemble from identical inputs share a CID, and any drift changes it. The CID
covers only the *deterministic inputs* (model, weights, budget, candidates, prompts) — never the
non-deterministic model completion, which is attached afterward. This is the substrate the
Replay Debugger and every other replay-derived view read from.

v1 uses BLAKE2b for the CID and SHA-256 for the prompt hash (stdlib, deterministic across runs);
production seals the body under the tenant DEK and uses BLAKE3 content addressing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, Field

from ..adapters.base import ChatMessage
from ..models.common import utcnow
from ..models.memory import MemoryCandidate


class BundleMessage(BaseModel):
    role: str
    content: str


def render_prompt_hash(messages: list[ChatMessage]) -> str:
    """SHA-256 over the canonical rendering of the final prompt — the bit-exactness anchor."""
    canonical = "\n".join(f"{m.role.value}\x1f{m.content}" for m in messages)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_address(
    *,
    model_id: str,
    mmr_lambda: float,
    weights: dict[str, float],
    budget: dict[str, int],
    system_prompt: str,
    latest_user_turn: str,
    candidates: list[MemoryCandidate],
) -> str:
    payload = {
        "model_id": model_id,
        "mmr_lambda": mmr_lambda,
        "weights": weights,
        "budget": budget,
        "system_prompt": system_prompt,
        "latest_user_turn": latest_user_turn,
        "candidates": [c.model_dump(mode="json") for c in candidates],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "b2:" + hashlib.blake2b(canonical.encode("utf-8"), digest_size=16).hexdigest()


class ContextBundle(BaseModel):
    schema_version: str = "contextos.bundle.v1"
    bundle_cid: str
    trace_id: str
    tenant_id: str
    namespace: str
    created_at: datetime = Field(default_factory=utcnow)
    model_id: str
    mmr_lambda: float
    weights: dict[str, float]
    budget: dict[str, int]
    system_prompt: str
    latest_user_turn: str
    candidates: list[MemoryCandidate]
    rendered_messages: list[BundleMessage]
    prompt_hash: str
    recorded_output: str | None = None  # the completion, attached post-generation (C8)
