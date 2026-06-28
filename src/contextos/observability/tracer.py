"""Minimal trace + decision-record substrate — the seed of the flagship Replay Debugger.

At the walking-skeleton stage this keeps spans in memory and logs them. The shape is the one
that matters: every stage opens a span, decision stages attach a ``DecisionRecord`` whose
``pointers`` will (at Month 1) reference content-addressed bundle blobs. ``replay`` is wired
later against those pointers; here we expose ``get_trace`` so the gateway can return the stub.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from ..models.common import new_ulid, utcnow
from ..models.trace import DecisionRecord, TraceSpan
from ..security.context import SecurityContext


class Trace:
    def __init__(self, trace_id: str, tenant_id: str) -> None:
        self.trace_id = trace_id
        self.tenant_id = tenant_id
        self.spans: list[TraceSpan] = []

    @contextmanager
    def span(self, stage: str, name: str, parent: str | None = None) -> Iterator[TraceSpan]:
        sp = TraceSpan(
            trace_id=self.trace_id, tenant_id=self.tenant_id, stage=stage, name=name, parent_span_id=parent
        )
        try:
            yield sp
        finally:
            sp.end = utcnow()
            self.spans.append(sp)

    @staticmethod
    def record(sp: TraceSpan, summary: str, **pointers: str) -> None:
        sp.decision = DecisionRecord(stage=sp.stage, summary=summary, pointers=dict(pointers))


class Tracer:
    """In-memory tracer. A production tracer flushes to the durable trace store via Redis
    Streams (best-effort, sampled) while cost records go to a fail-closed outbox (C12)."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}

    def start(self, ctx: SecurityContext) -> Trace:
        trace = Trace(trace_id=new_ulid(), tenant_id=ctx.tenant_id)
        self._traces[trace.trace_id] = trace
        return trace

    def get(self, ctx: SecurityContext, trace_id: str) -> Trace | None:
        trace = self._traces.get(trace_id)
        # Traces are tenant-scoped like everything else — no cross-tenant trace reads.
        if trace is None or trace.tenant_id != ctx.tenant_id:
            return None
        return trace
