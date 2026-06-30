"""The flagship: see — and replay — exactly what your LLM received.

    python examples/replay_demo.py

Uses an echo backend that returns the assembled prompt, so you can literally read what the
model saw, then reproduce that context decision bit-for-bit and diff two requests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from contextos import ContextOS
from contextos.adapters.base import (
    Capabilities,
    ChatRequest,
    ChatResponse,
    StreamEvent,
    StreamEventType,
    Usage,
)


class EchoBackend:
    """A backend that returns the prompt it was given — so the demo can show the injected context."""

    name = "echo"

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def health_check(self) -> bool:
        return True

    async def generate(self, req: ChatRequest) -> ChatResponse:
        text = "  ".join(m.content for m in req.messages)
        return ChatResponse(text=text, model=req.model, finish_reason="stop", usage=Usage())

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


def main() -> None:
    ctx = ContextOS(user_id="u", tenant="acme", adapter=EchoBackend())
    ctx.remember("user's prod region is eu-west-1")
    ctx.remember("billing currency is EUR")

    r1 = ctx.chat("which region is prod deployed in?")
    print("=== what the model actually received ===")
    print(r1)
    print()

    rep = ctx.replay(r1.trace_id)
    assert rep is not None
    print("replay prompt_equal :", rep.prompt_equal, "(bit-for-bit)")
    print("replay output_equal :", rep.output_equal)
    print("bundle_cid          :", rep.bundle_cid)

    # Ask something different, then diff the two context decisions.
    r2 = ctx.chat("what currency do we bill in?")
    diff = ctx.diff(r1.trace_id, r2.trace_id)
    assert diff is not None
    print()
    print("context diff  prompt_changed :", diff.prompt_changed)
    print("              candidates +/- :", diff.candidates_added, diff.candidates_removed)


if __name__ == "__main__":
    main()
