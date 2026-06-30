"""ContextOS 5-minute quickstart — runs fully offline with a deterministic fake backend.

    python examples/quickstart.py

No GPU, no API key, no database: this is the same pipeline the gateway runs, in-process.
"""

from __future__ import annotations

from contextos import ContextOS
from contextos.adapters.fake import FakeAdapter


def main() -> None:
    # A fake backend keeps the demo offline; point ContextOS at vLLM/Ollama/OpenAI in real use.
    ctx = ContextOS(user_id="123", tenant="acme", adapter=FakeAdapter("Deploy with the vLLM Helm chart."))

    # 1) Teach it durable facts about this user. They persist across calls.
    ctx.remember("user's prod region is eu-west-1")
    ctx.remember("the team deploys with Helm on Kubernetes")

    # 2) Ask. Memory is retrieved, ranked, budget-packed, and injected automatically.
    reply = ctx.chat("how should we deploy our LLM?")
    print("reply        :", reply)
    print("trace_id     :", reply.trace_id)

    # 3) The flagship: reproduce that exact context decision, bit-for-bit.
    rep = ctx.replay(reply.trace_id)
    assert rep is not None
    print("replay       : prompt reproduced bit-for-bit =", rep.prompt_equal)
    print("bundle_cid   :", rep.bundle_cid)

    # 4) Dollars are a first-class metric.
    cost = ctx.cost()
    print("cost         :", f"${cost.total_cost_usd:.6f} over {cost.requests} request(s)")


if __name__ == "__main__":
    main()
