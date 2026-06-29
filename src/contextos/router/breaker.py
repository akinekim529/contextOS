"""Per-backend circuit breaker: stop sending traffic to a failing backend, probe to recover.

Closed → (failures reach threshold) → Open → (cooldown elapses) → Half-open → (success) → Closed.
The clock is injectable so the state machine is deterministically testable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum

from ..models.common import utcnow


class BreakerState(str, Enum):
    CLOSED = "closed"        # healthy — traffic flows
    OPEN = "open"            # tripped — traffic skipped until cooldown
    HALF_OPEN = "half_open"  # cooldown elapsed — allow a probe


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._now = now
        self._failures = 0
        self._opened_at: datetime | None = None

    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._now() - self._opened_at >= timedelta(seconds=self._cooldown):
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allows(self) -> bool:
        """True unless the breaker is fully open (half-open allows a probe)."""
        return self.state() is not BreakerState.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._now()
