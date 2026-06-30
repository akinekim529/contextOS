"""Model Router v1 — cost/quality/latency optimization with fail-closed policy filters.

Decision order (per spec 2.6 and C9):
  1. HARD filters on STATIC policy — allowlist (via the RBAC firewall, action=route, C10),
     residency, capability, budget. These fail CLOSED, independent of backend health: a tenant
     policy can never be bypassed by an outage.
  2. OPTIMIZATION over the survivors — utility trades pool-normalized cost against quality, with
     difficulty shifting weight toward quality for hard queries. These signals fail OPEN: if all
     breakers are open we fall back to the static ranking of the (still hard-filtered) pool, so
     the safe default can never violate residency/allowlist.

Routing is deterministic (no randomness) so the choice is replayable (C7). Ties break by name.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.base import BackendAdapter
from ..models.common import Action
from ..security.context import SecurityContext
from ..security.errors import AccessDenied
from ..security.rbac import PolicyEngine
from .breaker import CircuitBreaker
from .difficulty import estimate_difficulty
from .types import BackendSpec, NoEligibleBackend, RouteDecision

COST_WEIGHT = 0.6      # cheap-vs-quality trade-off strength; crossover difficulty ~0.66
LATENCY_WEIGHT = 0.05  # light tie-breaker


@dataclass
class RegistryEntry:
    spec: BackendSpec
    adapter: BackendAdapter
    breaker: CircuitBreaker


class BackendRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, RegistryEntry] = {}

    def register(
        self, spec: BackendSpec, adapter: BackendAdapter, *, breaker: CircuitBreaker | None = None
    ) -> None:
        self._by_name[spec.name] = RegistryEntry(spec, adapter, breaker or CircuitBreaker())

    def get(self, name: str) -> RegistryEntry | None:
        return self._by_name.get(name)

    def all(self) -> list[RegistryEntry]:
        return list(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)


class ModelRouter:
    def __init__(self, policy: PolicyEngine | None = None) -> None:
        self._policy = policy or PolicyEngine()

    def route(
        self,
        ctx: SecurityContext,
        query: str,
        *,
        assembled_tokens: int,
        registry: BackendRegistry,
        max_cost_per_1k: float | None = None,
        residency: str | None = None,
        required_capabilities: tuple[str, ...] = (),
    ) -> RouteDecision:
        difficulty = estimate_difficulty(query, assembled_tokens)
        reasons: list[str] = [f"difficulty={difficulty:.2f}"]

        # 1) HARD filters on static policy — fail closed.
        eligible: list[RegistryEntry] = []
        for e in registry.all():
            try:
                self._policy.check(
                    ctx,
                    resource={"type": "model", "tenant_id": ctx.tenant_id, "model": e.spec.model_id},
                    action=Action.ROUTE,
                )
            except AccessDenied:
                reasons.append(f"{e.spec.name}: policy-denied")
                continue
            if residency and e.spec.residency != residency:
                # A required residency is a hard data-locality constraint; "any" does not satisfy it.
                reasons.append(f"{e.spec.name}: residency {e.spec.residency}!={residency}")
                continue
            if max_cost_per_1k is not None and e.spec.cost_per_1k > max_cost_per_1k:
                reasons.append(f"{e.spec.name}: over-budget")
                continue
            if any(cap not in e.spec.capabilities for cap in required_capabilities):
                reasons.append(f"{e.spec.name}: missing-capability")
                continue
            eligible.append(e)

        if not eligible:
            raise NoEligibleBackend("no backend satisfies the hard policy filters")

        # 2) OPTIMIZATION — skip open breakers; if all are open, fall back to the static pool.
        healthy = [e for e in eligible if e.breaker.allows()]
        pool = healthy or eligible
        from_safe_default = not healthy
        if from_safe_default:
            reasons.append("all-breakers-open: safe-default ranking (hard filters still hold)")

        # Normalize cost & latency within the pool so the trade-off is scale-free: difficulty
        # shifts weight from cost (easy -> cheap) toward quality (hard -> best model).
        max_cost = max((e.spec.cost_per_1k for e in pool), default=0.0)
        max_lat = max((e.spec.p50_latency_ms for e in pool), default=0.0)

        def score(e: RegistryEntry) -> float:
            nc = e.spec.cost_per_1k / max_cost if max_cost > 0 else 0.0
            nl = e.spec.p50_latency_ms / max_lat if max_lat > 0 else 0.0
            return difficulty * e.spec.quality - COST_WEIGHT * (1.0 - difficulty) * nc - LATENCY_WEIGHT * nl

        ranked = sorted(pool, key=lambda e: (score(e), e.spec.name), reverse=True)
        chosen = ranked[0]
        return RouteDecision(
            backend=chosen.spec.name,
            model_id=chosen.spec.model_id,
            utility=round(score(chosen), 6),
            difficulty=round(difficulty, 4),
            fallback_chain=[e.spec.name for e in ranked[1:]],
            reasons=reasons,
            from_safe_default=from_safe_default,
        )
