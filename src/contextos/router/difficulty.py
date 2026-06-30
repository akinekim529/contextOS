"""Two-stage difficulty estimation, stage one: a cheap, deterministic heuristic.

Routes easy queries to cheap models and hard ones to quality models. Deterministic on purpose
(no model call, no randomness) so the routing decision stays replayable (C7). A distilled
classifier is the documented stage-two upgrade; the heuristic is the shipped, real default.
"""

from __future__ import annotations

import re

_HARD = re.compile(
    r"\b(prove|derive|analyz|debug|optimi[sz]e|architect|design|refactor|reason|"
    r"explain why|step by step|trade-?off)\w*",
    re.IGNORECASE,
)


def estimate_difficulty(query: str, assembled_tokens: int) -> float:
    """Return a difficulty score in [0, 1]."""
    words = len(query.split())
    length_signal = min(1.0, words / 60.0)
    token_signal = min(1.0, assembled_tokens / 4000.0)
    keyword_signal = 0.30 if _HARD.search(query) else 0.0
    return min(1.0, 0.45 * length_signal + 0.35 * token_signal + keyword_signal)
