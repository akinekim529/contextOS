"""Token budget with inviolable hard reserves.

The system prompt and the latest user turn are HARD reserves: they are never evicted or
truncated to fit retrieved context. If the hard reserves plus the output reservation do not
fit the model's window, the request cannot be satisfied and we fail closed with HTTP 413 —
before spending any work on packing. Everything left over is the soft budget the knapsack fills.
"""

from __future__ import annotations

from dataclasses import dataclass


class ContextOverflow(Exception):
    """Hard-reserve overflow -> HTTP 413. We never silently truncate a hard reserve."""


@dataclass(frozen=True)
class TokenBudget:
    window_tokens: int
    output_reserve: int        # reserved for the completion (hard)
    system_reserve: int        # system prompt — HARD, never evicted
    latest_user_reserve: int   # latest user turn — HARD, never evicted

    @property
    def hard_floor(self) -> int:
        return self.output_reserve + self.system_reserve + self.latest_user_reserve

    @property
    def soft_budget(self) -> int:
        """Tokens available for memory / docs / history after the inviolable reserves."""
        return max(0, self.window_tokens - self.hard_floor)


def check_hard_reserves(b: TokenBudget) -> None:
    if b.hard_floor > b.window_tokens:
        raise ContextOverflow(
            f"hard reserves ({b.hard_floor} tok) exceed window ({b.window_tokens} tok); "
            "cannot satisfy without truncating the system prompt or the user's turn"
        )
