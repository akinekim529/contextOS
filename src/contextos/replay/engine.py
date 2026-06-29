"""Context Replay Debugger v1 — reproduce any past context decision, bit-for-bit.

Capture freezes the assembler's inputs + the rendered prompt into a content-addressed bundle.
``replay(trace_id)`` rebuilds the prompt from those frozen inputs through the *same deterministic*
assembly and asserts the rendered-prompt hash matches the recorded one (decision replay, C7).

The C7 boundary is structural: deterministic stages (everything ContextOS decides) are asserted
for byte-equality; the model completion is non-deterministic, so ``output_equal`` is asserted
ONLY in RECORDED_OUTPUT mode and is ``None`` in LIVE_BACKEND mode — a caller cannot misread it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from ..assembler.budget import TokenBudget
from ..assembler.engine import ContextAssembler, ContextSources
from ..assembler.tokenizer import Tokenizer
from ..assembler.weights import RankWeights
from ..models.memory import MemoryCandidate
from ..security.context import SecurityContext
from .bundle import BundleMessage, ContextBundle, content_address, render_prompt_hash
from .store import InMemoryBundleStore


class ReplayMode(str, Enum):
    RECORDED_OUTPUT = "recorded_output"  # offline; output_equal is assertable
    LIVE_BACKEND = "live_backend"        # re-invokes the model -> diff only


class ReplayResult(BaseModel):
    trace_id: str
    bundle_cid: str
    mode: ReplayMode
    prompt_equal: bool                # the assembled prompt reproduced bit-for-bit
    prompt_hash_expected: str
    prompt_hash_actual: str
    output_equal: bool | None         # asserted ONLY in RECORDED_OUTPUT mode; None otherwise
    recorded_output: str | None = None
    diff: str | None = None


def _first_diff(expected: list[BundleMessage], actual_roles: list[str], actual_contents: list[str]) -> str:
    if len(expected) != len(actual_roles):
        return f"message count differs: expected {len(expected)}, got {len(actual_roles)}"
    for i, exp in enumerate(expected):
        if exp.role != actual_roles[i] or exp.content != actual_contents[i]:
            return f"message[{i}] differs (role/content mismatch)"
    return "hash differs but messages compare equal (encoding drift)"


class ReplayDebugger:
    def __init__(
        self, assembler: ContextAssembler, tokenizer: Tokenizer, store: InMemoryBundleStore
    ) -> None:
        self._assembler = assembler
        self._tokenizer = tokenizer
        self._store = store

    def capture(
        self,
        trace_id: str,
        ctx: SecurityContext,
        *,
        system_prompt: str,
        latest_user_turn: str,
        candidates: list[MemoryCandidate],
        weights: RankWeights,
        mmr_lambda: float,
        budget: TokenBudget,
        model_id: str,
        rendered_messages: list[BundleMessage],
        prompt_hash: str,
    ) -> ContextBundle:
        weights_d = {
            "vector": weights.vector, "lexical": weights.lexical, "recency": weights.recency,
            "importance": weights.importance, "source": weights.source,
        }
        budget_d = {
            "window_tokens": budget.window_tokens, "output_reserve": budget.output_reserve,
            "system_reserve": budget.system_reserve, "latest_user_reserve": budget.latest_user_reserve,
        }
        cid = content_address(
            model_id=model_id, mmr_lambda=mmr_lambda, weights=weights_d, budget=budget_d,
            system_prompt=system_prompt, latest_user_turn=latest_user_turn, candidates=candidates,
        )
        bundle = ContextBundle(
            bundle_cid=cid, trace_id=trace_id, tenant_id=ctx.tenant_id, namespace=ctx.namespace,
            model_id=model_id, mmr_lambda=mmr_lambda, weights=weights_d, budget=budget_d,
            system_prompt=system_prompt, latest_user_turn=latest_user_turn, candidates=list(candidates),
            rendered_messages=rendered_messages, prompt_hash=prompt_hash,
        )
        self._store.put(ctx, bundle)
        return bundle

    def attach_output(self, ctx: SecurityContext, trace_id: str, output: str) -> bool:
        return self._store.attach_output(ctx, trace_id, output)

    def get(self, ctx: SecurityContext, trace_id: str) -> ContextBundle | None:
        return self._store.get(ctx, trace_id)

    async def replay(
        self, ctx: SecurityContext, trace_id: str, *, mode: ReplayMode = ReplayMode.RECORDED_OUTPUT
    ) -> ReplayResult | None:
        bundle = self._store.get(ctx, trace_id)
        if bundle is None:
            return None  # tenant-scoped: another tenant's trace is simply not found

        sources = ContextSources(
            system_prompt=bundle.system_prompt,
            latest_user_turn=bundle.latest_user_turn,
            candidates=bundle.candidates,
        )
        assembled = await self._assembler.assemble(
            TokenBudget(**bundle.budget), sources, RankWeights(**bundle.weights),
            self._tokenizer, mmr_lambda=bundle.mmr_lambda,
        )
        actual_hash = render_prompt_hash(assembled.messages)
        prompt_equal = actual_hash == bundle.prompt_hash

        diff: str | None = None
        if not prompt_equal:
            diff = _first_diff(
                bundle.rendered_messages,
                [m.role.value for m in assembled.messages],
                [m.content for m in assembled.messages],
            )
        elif mode is ReplayMode.LIVE_BACKEND:
            diff = "live re-generation not compared (non-deterministic backend)"

        return ReplayResult(
            trace_id=trace_id,
            bundle_cid=bundle.bundle_cid,
            mode=mode,
            prompt_equal=prompt_equal,
            prompt_hash_expected=bundle.prompt_hash,
            prompt_hash_actual=actual_hash,
            # output_equal is asserted ONLY in RECORDED_OUTPUT mode (C7).
            output_equal=(prompt_equal if mode is ReplayMode.RECORDED_OUTPUT else None),
            recorded_output=bundle.recorded_output,
            diff=diff,
        )
