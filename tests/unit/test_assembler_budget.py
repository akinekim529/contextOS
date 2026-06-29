"""Token budget + hard-reserve fail-closed semantics."""

from __future__ import annotations

import pytest

from contextos.assembler.budget import ContextOverflow, TokenBudget, check_hard_reserves


def test_floor_and_soft_budget() -> None:
    b = TokenBudget(window_tokens=1000, output_reserve=200, system_reserve=50, latest_user_reserve=100)
    assert b.hard_floor == 350
    assert b.soft_budget == 650
    check_hard_reserves(b)  # fits -> does not raise


def test_overflow_fails_closed() -> None:
    b = TokenBudget(window_tokens=100, output_reserve=80, system_reserve=30, latest_user_reserve=40)
    assert b.hard_floor == 150
    assert b.soft_budget == 0  # never negative
    with pytest.raises(ContextOverflow):
        check_hard_reserves(b)
