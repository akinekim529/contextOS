"""Context diff — compare two context bundles and show exactly what changed.

A view over the replay substrate ("everything is a view over the bundle"): given two trace
bundles it reports which candidate memories entered/left the context, whether the routed model
changed, and whether the rendered prompt changed (with both prompts for inspection).
"""

from __future__ import annotations

from dataclasses import dataclass

from .bundle import ContextBundle


@dataclass(frozen=True)
class ContextDiff:
    candidates_added: list[str]
    candidates_removed: list[str]
    model_changed: tuple[str, str] | None
    prompt_changed: bool
    prompt_a: str
    prompt_b: str


def _render(bundle: ContextBundle) -> str:
    return "\n".join(f"{m.role}: {m.content}" for m in bundle.rendered_messages)


def diff_bundles(a: ContextBundle, b: ContextBundle) -> ContextDiff:
    a_ids = {c.memory_id for c in a.candidates}
    b_ids = {c.memory_id for c in b.candidates}
    return ContextDiff(
        candidates_added=sorted(b_ids - a_ids),
        candidates_removed=sorted(a_ids - b_ids),
        model_changed=(a.model_id, b.model_id) if a.model_id != b.model_id else None,
        prompt_changed=a.prompt_hash != b.prompt_hash,
        prompt_a=_render(a),
        prompt_b=_render(b),
    )
