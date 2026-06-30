"""Context diff over replay bundles."""

from __future__ import annotations

from contextos.models.common import MemoryTier
from contextos.models.memory import MemoryCandidate
from contextos.replay.bundle import BundleMessage, ContextBundle
from contextos.replay.diff import diff_bundles


def _cand(mid: str) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=mid, tenant_id="acme", namespace="alpha", tier=MemoryTier.SEMANTIC, content=mid
    )


def _bundle(cid: str, cands: list[MemoryCandidate], *, model: str = "m1", phash: str = "h") -> ContextBundle:
    return ContextBundle(
        bundle_cid=cid, trace_id="t", tenant_id="acme", namespace="alpha", model_id=model,
        mmr_lambda=0.7, weights={}, budget={}, system_prompt="", latest_user_turn="q",
        candidates=cands, rendered_messages=[BundleMessage(role="user", content="q")], prompt_hash=phash,
    )


def test_diff_bundles_reports_candidate_model_and_prompt_changes() -> None:
    a = _bundle("a", [_cand("m1"), _cand("m2")], phash="h1")
    b = _bundle("b", [_cand("m2"), _cand("m3")], model="m2", phash="h2")
    d = diff_bundles(a, b)
    assert d.candidates_added == ["m3"]
    assert d.candidates_removed == ["m1"]
    assert d.model_changed == ("m1", "m2")
    assert d.prompt_changed is True


def test_diff_identical_bundles_is_empty() -> None:
    a = _bundle("a", [_cand("m1")], phash="h")
    b = _bundle("a", [_cand("m1")], phash="h")
    d = diff_bundles(a, b)
    assert not d.candidates_added and not d.candidates_removed
    assert d.model_changed is None and d.prompt_changed is False
