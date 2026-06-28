"""Trace spans and decision records — OpenTelemetry-compatible, and the seed of replay.

Every pipeline stage emits a span; stages that *decide* something (which memories, which
model, cache hit/miss) attach a ``DecisionRecord`` with pointers into the content-addressed
context bundle. ``replay(trace_id)`` walks these to reproduce the decision bit-for-bit.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .common import new_ulid, utcnow


class DecisionRecord(BaseModel):
    """The 'why' behind a stage. ``pointers`` reference bundle blobs (candidate set,
    score vector, rendered prompt hash) by content address."""

    stage: str
    summary: str
    pointers: dict[str, str] = Field(default_factory=dict)


class TraceSpan(BaseModel):
    trace_id: str
    span_id: str = Field(default_factory=new_ulid)
    parent_span_id: str | None = None
    tenant_id: str
    stage: str = Field(..., description="auth|cache|retrieve|assemble|route|backend.invoke|writeback")
    name: str
    start: datetime = Field(default_factory=utcnow)
    end: datetime | None = None
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)
    decision: DecisionRecord | None = None

    def duration_ms(self) -> float | None:
        if self.end is None:
            return None
        return (self.end - self.start).total_seconds() * 1000.0
