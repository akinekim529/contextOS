# ContextOS

**The context & memory operating system for LLM applications.**

ContextOS is middleware that sits between your application and any LLM backend (vLLM, Ollama, TGI, OpenAI, Anthropic, Azure). It owns the part everyone rebuilds in-house: **deciding what goes into the context window** — which memories, which cached answer, which model — as a single, per-tenant, token-budget-constrained, zero-trust, *fully replayable* runtime decision.

It is **not** an LLM, an inference engine, a vector database, a training system, or an agent framework. It forwards to those and makes the decisions around them auditable.

```python
from contextos import ContextOS

ctx = ContextOS(user_id="123", tenant="acme")
response = ctx.chat("how do I deploy an LLM on Kubernetes?")
```

That's the whole simple path. Memory, retrieval, semantic caching, model routing, RBAC isolation, and tracing happen underneath. When you need control, every one of those is a knob — see [the API design](docs/design/03-api-design.md).

## The wedge

mem0/Zep own memory. GPTCache owns caching. LiteLLM/OpenRouter own routing. Langfuse/Helicone own observability. LangChain/LlamaIndex own orchestration. **None of them treat the context window as one joint decision** — select memory *and* check the cache *and* pick the model, under a tenant's token budget and isolation policy, in a way you can replay byte-for-byte afterward. ContextOS does, and that joint, auditable decision is the thing serious LLM platforms otherwise build themselves.

## 🏴 Flagship — the Context Replay Debugger

Every request emits a content-addressed, per-tenant-encrypted **context bundle**: the exact candidate memories and their scores, the budget-packing decision, the route choice, the cache verdict, the rendered prompt. `replay(trace_id)` reconstructs that decision **bit-for-bit** — so "why did the model say that?" stops being a guess. It is the shared substrate every other feature (cost dashboard, memory versioning, context diff) emits into.

## Headline targets

| Metric | Target |
|---|---|
| Added context-assembly latency | **< 50 ms p95** (excl. inference) |
| Memory retrieval | **< 100 ms p95** |
| Total ContextOS control overhead | **< 250 ms p95** |
| Gateway throughput | **5k–10k req/s per node** |
| Control/gateway availability | **99.9%** |
| Cross-tenant leaks | **0** (RLS + app firewall; CI gate ≥10k hostile probes) |
| Cache hit-ratio / token-cost savings | **25–45% / 40–65%** |

These are design targets; see [the NFR section](docs/design/08-nfr.md) for the authoritative per-stage budget and the methodology behind each number.

## Status

Pre-alpha. The full design is in [`docs/design/`](docs/design/) (start with the [executive summary](docs/design/00-executive-summary.md)); architecture decisions are recorded in [`docs/adr/`](docs/adr/). The build follows the [roadmap](docs/design/09-roadmap.md): a walking skeleton (gateway + zero-trust isolation + one adapter + trace stub) first, the flagship replay MVP at Month 1, production HA/security at Month 3.

## License

Apache-2.0.
