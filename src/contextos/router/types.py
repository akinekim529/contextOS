"""Model Router value types.

A backend is described by a static :class:`BackendSpec` (cost/quality/latency/residency/caps).
A routing decision is the deterministic, replayable output the rest of the pipeline consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class NoEligibleBackend(Exception):
    """No backend satisfies the hard policy filters (allowlist/residency/capability/budget).

    Fail-closed: we never route to a backend a tenant policy forbids, even if everything is down.
    """


class BackendUnavailable(Exception):
    """Every routed backend (chosen + fallback chain) failed or is breaker-open."""


@dataclass(frozen=True)
class BackendSpec:
    name: str                              # registry key (also the adapter's name)
    model_id: str
    cost_per_1k: float                     # blended USD per 1k tokens
    quality: float                         # 0..1 quality prior
    p50_latency_ms: float
    residency: str = "any"                 # "eu" | "us" | "any"
    capabilities: tuple[str, ...] = ()     # e.g. ("streaming", "tools")
    tags: tuple[str, ...] = ()             # allowlist tags


@dataclass(frozen=True)
class RouteDecision:
    backend: str
    model_id: str
    utility: float
    difficulty: float
    fallback_chain: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    from_safe_default: bool = False
