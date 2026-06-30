"""Cost ledger — dollars tracked with the same rigor as latency (design philosophy #4).

Per-request cost is computed from a price table and the model's token usage, recorded
tenant-scoped, and rolled up per model. Tenant partitioning means one tenant's spend is never
visible to another. A production deployment swaps the in-memory ledger for the fail-closed cost
outbox (C12); the interface is the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..adapters.base import Usage
from ..security.context import SecurityContext

# Blended USD per 1k tokens. Real deployments override via build_cost_ledger(prices=...).
DEFAULT_PRICES: dict[str, float] = {
    "default": 0.0,
    "haiku": 0.001,
    "opus": 0.02,
    "gpt-4o-mini": 0.0006,
    "meta-llama/Llama-3.1-8B-Instruct": 0.0002,
}


@dataclass(frozen=True)
class CostRecord:
    tenant_id: str
    model_id: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class CostSummary:
    tenant_id: str
    requests: int
    total_tokens: int
    total_cost_usd: float
    by_model: dict[str, float] = field(default_factory=dict)


class CostLedger:
    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self._prices = {**DEFAULT_PRICES, **(prices or {})}
        self._by_tenant: dict[str, list[CostRecord]] = {}

    def price_per_1k(self, model_id: str) -> float:
        return self._prices.get(model_id, self._prices.get("default", 0.0))

    def record(self, ctx: SecurityContext, model_id: str, usage: Usage) -> CostRecord:
        total = usage.prompt_tokens + usage.completion_tokens
        cost = round(self.price_per_1k(model_id) * total / 1000.0, 6)
        rec = CostRecord(ctx.tenant_id, model_id, usage.prompt_tokens, usage.completion_tokens, cost)
        self._by_tenant.setdefault(ctx.tenant_id, []).append(rec)
        return rec

    def summary(self, ctx: SecurityContext) -> CostSummary:
        recs = self._by_tenant.get(ctx.tenant_id, [])  # tenant-scoped: no cross-tenant spend leaks
        by_model: dict[str, float] = {}
        for r in recs:
            by_model[r.model_id] = round(by_model.get(r.model_id, 0.0) + r.cost_usd, 6)
        return CostSummary(
            tenant_id=ctx.tenant_id,
            requests=len(recs),
            total_tokens=sum(r.prompt_tokens + r.completion_tokens for r in recs),
            total_cost_usd=round(sum(r.cost_usd for r in recs), 6),
            by_model=by_model,
        )
