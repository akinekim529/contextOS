"""Typed error envelope. Every error response carries a ``trace_id`` so a failure is as
debuggable as a success — observability-first, even on the error path."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorBody(BaseModel):
    type: str
    message: str
    trace_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def envelope(type_: str, message: str, trace_id: str | None = None) -> dict[str, Any]:
    return ErrorEnvelope(error=ErrorBody(type=type_, message=message, trace_id=trace_id)).model_dump()
