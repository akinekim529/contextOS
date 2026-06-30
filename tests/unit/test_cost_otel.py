"""Cost ledger (tenant-scoped) and OTel export shape."""

from __future__ import annotations

from contextos.adapters.base import Usage
from contextos.models.common import utcnow
from contextos.models.trace import TraceSpan
from contextos.observability.cost import CostLedger
from contextos.observability.otel import to_otlp
from helpers import make_ctx


def test_cost_record_and_summary() -> None:
    ledger = CostLedger(prices={"m1": 2.0})  # $2 per 1k tokens
    ctx = make_ctx("acme", "u", "alpha")
    ledger.record(ctx, "m1", Usage(prompt_tokens=400, completion_tokens=600))  # 1000 tok -> $2
    ledger.record(ctx, "m1", Usage(prompt_tokens=500, completion_tokens=0))    # 500 tok -> $1
    s = ledger.summary(ctx)
    assert s.requests == 2
    assert s.total_tokens == 1500
    assert s.total_cost_usd == 3.0
    assert s.by_model["m1"] == 3.0


def test_cost_is_tenant_scoped() -> None:
    ledger = CostLedger(prices={"m1": 1.0})
    ledger.record(make_ctx("a", "u", "x"), "m1", Usage(prompt_tokens=1000, completion_tokens=0))
    other = ledger.summary(make_ctx("b", "u", "x"))
    assert other.requests == 0 and other.total_cost_usd == 0.0


def test_to_otlp_shape() -> None:
    span = TraceSpan(trace_id="t1", tenant_id="acme", stage="route", name="model-router")
    span.end = utcnow()
    payload = to_otlp([span])
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[0]["traceId"] == "t1" and spans[0]["name"] == "model-router"
    keys = {a["key"] for a in spans[0]["attributes"]}
    assert "tenant_id" in keys and "stage" in keys
    assert spans[0]["startTimeUnixNano"] > 0
