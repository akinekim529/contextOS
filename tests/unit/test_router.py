"""Model Router v1: difficulty, circuit breaker, cost/quality routing, fail-closed filters, fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import pytest

from contextos.adapters.base import Capabilities, ChatRequest, ChatResponse, StreamEvent, StreamEventType
from contextos.adapters.fake import FakeAdapter
from contextos.router.breaker import BreakerState, CircuitBreaker
from contextos.router.difficulty import estimate_difficulty
from contextos.router.engine import BackendRegistry, ModelRouter
from contextos.router.types import BackendSpec, NoEligibleBackend
from helpers import make_ctx

CHEAP = BackendSpec(name="cheap", model_id="haiku", cost_per_1k=0.001, quality=0.6, p50_latency_ms=200)
QUALITY = BackendSpec(name="quality", model_id="opus", cost_per_1k=0.02, quality=0.95, p50_latency_ms=900)
EU = BackendSpec(
    name="eu", model_id="m-eu", cost_per_1k=0.01, quality=0.8, p50_latency_ms=400, residency="eu"
)


def _registry(*specs: BackendSpec, factory: Callable[[], object] = FakeAdapter) -> BackendRegistry:
    reg = BackendRegistry()
    for s in specs:
        reg.register(s, factory())  # type: ignore[arg-type]
    return reg


class _Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t


def test_difficulty_easy_vs_hard() -> None:
    assert estimate_difficulty("hi there", 50) < 0.3
    hard = "explain in detail and derive why this distributed architecture deadlocks, step by step"
    assert estimate_difficulty(hard, 3500) > 0.5


def test_breaker_open_then_half_open_then_closed() -> None:
    clock = _Clock()
    b = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, now=clock.now)
    assert b.allows()
    b.record_failure()
    assert b.allows()                       # 1 < threshold
    b.record_failure()
    assert not b.allows() and b.state() is BreakerState.OPEN
    clock.t = clock.t + timedelta(seconds=11)
    assert b.allows() and b.state() is BreakerState.HALF_OPEN
    b.record_success()
    assert b.state() is BreakerState.CLOSED


def test_easy_query_routes_to_cheap() -> None:
    d = ModelRouter().route(
        make_ctx("acme", "u", "alpha"), "hi", assembled_tokens=40, registry=_registry(CHEAP, QUALITY)
    )
    assert d.backend == "cheap"
    assert d.fallback_chain == ["quality"]


def test_hard_query_routes_to_quality() -> None:
    hard = "derive and prove step by step why the architecture deadlocks and design a refactor"
    d = ModelRouter().route(
        make_ctx("acme", "u", "alpha"), hard, assembled_tokens=3800, registry=_registry(CHEAP, QUALITY)
    )
    assert d.backend == "quality"


def test_residency_and_budget_are_hard_filters() -> None:
    r = ModelRouter()
    ctx = make_ctx("acme", "u", "alpha")
    assert r.route(
        ctx, "hi", assembled_tokens=40, registry=_registry(CHEAP, EU), residency="eu"
    ).backend == "eu"
    with pytest.raises(NoEligibleBackend):
        r.route(ctx, "hi", assembled_tokens=40, registry=_registry(CHEAP, QUALITY), residency="eu")
    assert r.route(
        ctx, "hi", assembled_tokens=40, registry=_registry(CHEAP, QUALITY), max_cost_per_1k=0.005
    ).backend == "cheap"


def test_all_breakers_open_uses_safe_default_but_keeps_hard_filters() -> None:
    reg = _registry(CHEAP, QUALITY)
    for name in ("cheap", "quality"):
        for _ in range(5):
            reg.get(name).breaker.record_failure()  # type: ignore[union-attr]
    d = ModelRouter().route(make_ctx("acme", "u", "alpha"), "hi", assembled_tokens=40, registry=reg)
    assert d.from_safe_default is True
    # residency still enforced even when falling back to the safe-default pool
    with pytest.raises(NoEligibleBackend):
        ModelRouter().route(
            make_ctx("acme", "u", "alpha"), "hi", assembled_tokens=40, registry=reg, residency="eu"
        )


class _FailingAdapter:
    name = "failing"

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def health_check(self) -> bool:
        return True

    async def generate(self, req: ChatRequest) -> ChatResponse:
        raise RuntimeError("backend down")

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


@pytest.mark.asyncio
async def test_pipeline_routes_and_falls_over() -> None:
    from contextos.pipeline import Pipeline

    reg = BackendRegistry()
    reg.register(CHEAP, _FailingAdapter())            # chosen for the easy query, but fails
    reg.register(QUALITY, FakeAdapter("from quality"))  # fallback
    pipe = Pipeline(adapter=FakeAdapter(), router=ModelRouter(), backends=reg)
    res = await pipe.run(make_ctx("acme", "u", "alpha"), "hi")
    assert res.text == "from quality"  # fell over to the fallback backend
