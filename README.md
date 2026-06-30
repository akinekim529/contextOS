# ContextOS

**See — and replay — exactly why your LLM said that.**

ContextOS is middleware between your app and any LLM backend (vLLM, Ollama, TGI, OpenAI, Anthropic, Azure). It owns the part every team rebuilds in-house: deciding what goes into the context window — which memories, which cached answer, which model — as one **per-tenant, token-budget-constrained, zero-trust, fully-replayable** runtime decision.

It is **not** an LLM, an inference engine, a vector DB, a training system, or an agent framework. It forwards to those and makes the decisions around them auditable.

```python
from contextos import ContextOS

ctx = ContextOS(user_id="123", tenant="acme")
ctx.remember("user's prod region is eu-west-1")
response = ctx.chat("how should we deploy our LLM?")   # memory retrieved, ranked, injected
print(ctx.replay(response.trace_id).prompt_equal)       # True — reproduced bit-for-bit
```

## 🏴 The flagship — Context Replay Debugger

Every request freezes a content-addressed, per-tenant context **bundle**: the exact candidate memories and scores, the budget-packing, the route choice, the cache verdict, the rendered prompt. `replay(trace_id)` reconstructs that decision **bit-for-bit**. "Why did the model say that?" stops being a guess.

```text
$ python examples/replay_demo.py
=== what the model actually received ===
Relevant context:
- user's prod region is eu-west-1
- billing currency is EUR  which region is prod deployed in?

replay prompt_equal : True (bit-for-bit)
replay output_equal : True
bundle_cid          : b2:a438adf1e096bfdf92bde14f83e53474

context diff  prompt_changed : True
              candidates +/- : [] []
```

## The wedge

mem0/Zep own memory. GPTCache owns caching. LiteLLM/OpenRouter own routing. Langfuse/Helicone own observability. LangChain/LlamaIndex own orchestration. **None treat the context window as one joint, auditable decision** — select memory *and* check the cache *and* pick the model, under a tenant's budget and isolation policy, in a way you can replay afterward. ContextOS does. See the [full comparison](docs/comparison.md).

## 5-minute quickstart

```bash
pip install -e ".[dev]"          # or: uv sync --extra dev
python examples/quickstart.py    # runs fully offline (fake backend)
python examples/replay_demo.py
pytest -m "not integration" -q   # 73 tests incl. the >=10k tenant-isolation gate
```

```text
$ python examples/quickstart.py
reply        : Deploy with the vLLM Helm chart.
trace_id     : 01KWCXDH3KET8XE0NRZCK6ZVDD
replay       : prompt reproduced bit-for-bit = True
bundle_cid   : b2:099a6692bdef08e56622e337e7643cab
cost         : $0.000006 over 1 request(s)
```

Point it at a real backend with env vars (`CONTEXTOS_BACKEND_KIND=vllm`, `CONTEXTOS_BACKEND_BASE_URL=...`) or run the gateway: `uvicorn contextos.gateway.asgi:app`.

## What runs on every request

`auth → semantic cache (short-circuits on hit) → memory retrieve → compress → assemble (rank + budget-pack + edge-load + inject) → model router (cost/quality/latency + fallback) → backend → write-back (cache + cost + replay bundle)` — every stage traced, every decision replayable.

| Capability | Module |
|---|---|
| Four-tier memory, hybrid RRF retrieval, decay, consolidation | [memory](src/contextos/memory/), [workers](src/contextos/workers/) |
| Ranking + token-budget packing + edge-loading (one weight vocabulary) | [assembler](src/contextos/assembler/) |
| Structural/extractive/abstractive compression with a fact-retention guard | [compressor](src/contextos/compressor/) |
| Two-tier semantic cache, per-tenant, fail-open | [cache](src/contextos/cache/) |
| Cost/quality/latency routing, circuit breakers, fallback chains | [router](src/contextos/router/) |
| Zero-trust RBAC firewall (+ Postgres RLS backstop) | [security](src/contextos/security/) |
| Replay debugger, context diff, git-like memory versioning, OTel + cost | [replay](src/contextos/replay/), [versioning](src/contextos/versioning/), [observability](src/contextos/observability/) |

## Headline targets

`< 50 ms p95` context assembly · `< 100 ms p95` memory retrieval · `5k–10k req/s`/node · `99.9%` gateway availability · **0 cross-tenant leaks** (RLS + app firewall; CI gate ≥10k hostile probes) · cache hit-ratio `25–45%` · token-cost savings `40–65%`. See the [NFR budget](docs/design/08-nfr.md).

## Status — v0.1, pre-alpha but real

**Runs today, in-memory, tested** (`ruff` + `mypy --strict` + 73 `pytest`, zero placeholders): memory, assembler, compressor, cache, router, replay/diff/versioning, cost+OTel, gateway, SDK, the backend adapters, and the tenant-isolation gate. Everything in the quickstart works with no infrastructure.

**Designed and deferred** (in [docs/design/](docs/design/), not faked in code): Kubernetes HA, real KMS/crypto-shred, ML-based prompt-injection defense, the optional Rust hot-path kernel, and production vLLM/Postgres/Redis at scale. The Helm chart, Dockerfile, and RLS migration are the production scaffold (CI-validated). See [STATUS.md](STATUS.md) for the per-module maturity matrix and [the roadmap](docs/design/09-roadmap.md).

## Docs

The full design is in [docs/design/](docs/design/) (start with the [executive summary](docs/design/00-executive-summary.md)); decisions are recorded as [ADRs](docs/adr/). New here? [START_HERE.md](START_HERE.md).

## License

Apache-2.0.
