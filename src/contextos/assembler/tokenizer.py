"""Token counting for budget packing.

C3: the router selects the model (and thus its tokenizer) BEFORE final packing, so the
Assembler enforces the hard reserve with the correct tokenizer. When the model is not yet
knowable, we pack against a **conservative over-count** (+8% margin) so re-validation can only
ever find *more* room than we reserved — never less. The +8% is the single canonical C3 margin.

The v1 default is a dependency-free ~4-chars/token heuristic; production swaps in the model's
real tokenizer (e.g. tiktoken) behind this same ``Tokenizer`` protocol.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

TOKENIZER_SAFETY_MARGIN = 0.08  # canonical C3 conservative-estimate margin


@runtime_checkable
class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...


class HeuristicTokenizer:
    def __init__(self, chars_per_token: float = 4.0) -> None:
        self._cpt = chars_per_token

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, math.ceil(len(text) / self._cpt))


def conservative_count(
    tokenizer: Tokenizer | None, text: str, *, margin: float = TOKENIZER_SAFETY_MARGIN
) -> int:
    """Over-count by ``margin`` when the routed model's tokenizer isn't known yet (C3)."""
    base = (tokenizer or HeuristicTokenizer()).count(text)
    return math.ceil(base * (1.0 + margin))
