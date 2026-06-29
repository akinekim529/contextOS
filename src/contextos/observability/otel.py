"""OpenTelemetry export — render ContextOS spans as an OTLP/JSON payload.

The :class:`~contextos.models.trace.TraceSpan` is already OTel-shaped; this converts a trace's
spans into the OTLP ``resourceSpans`` envelope so any OTel collector can ingest them. No SDK
dependency is required to *produce* the payload — a deployment can POST it to an OTLP/HTTP
endpoint, or wire the optional opentelemetry-sdk exporter behind the same shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models.trace import TraceSpan


def _nanos(dt: datetime | None) -> int:
    return int(dt.timestamp() * 1_000_000_000) if dt is not None else 0


def _attrs(items: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in items.items()]


def _span(s: TraceSpan) -> dict[str, Any]:
    attributes = {"tenant_id": s.tenant_id, "stage": s.stage, **s.attributes}
    if s.decision is not None:
        attributes["decision.summary"] = s.decision.summary
    return {
        "traceId": s.trace_id,
        "spanId": s.span_id,
        "parentSpanId": s.parent_span_id or "",
        "name": s.name,
        "startTimeUnixNano": _nanos(s.start),
        "endTimeUnixNano": _nanos(s.end),
        "attributes": _attrs(attributes),
    }


def to_otlp(spans: list[TraceSpan]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attrs({"service.name": "contextos"})},
                "scopeSpans": [
                    {"scope": {"name": "contextos.pipeline"}, "spans": [_span(s) for s in spans]}
                ],
            }
        ]
    }
