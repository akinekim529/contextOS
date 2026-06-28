# ContextOS — Executive Summary

## Core Thesis (≤250 words)

Every LLM application secretly runs an operating system for one scarce, contested resource: the context window. Today that OS is improvised inline — memory lookups here, a cache there, routing logic in a helper module, observability bolted on after the fact — and it is rebuilt, badly, in every application. **ContextOS is open-source middleware that sits between any application and any LLM backend and owns the context window as a single, joint, per-tenant, budget-constrained, zero-trust, replayable decision.**

It speaks an OpenAI-compatible `/v1` surface, so adoption is a base-URL change. Behind that surface it runs one coherent pipeline: **auth/tenant → cache lookup → retrieve candidates → ACL/redaction → compression → assembly/packing → routing → adapter → stream → async write-back**. Memory, semantic caching, token-budget assembly, model routing, multi-tenant isolation, and replay-grade observability stop being five vendors and one prayer; they become co-designed stages sharing one tenant boundary, one weight vocabulary, one cost ledger, and one replay log.

The wedge is conceptual, not feature-additive: incumbents each own one slice (memory, cache, routing, observability, orchestration) and **none treat the assembled prompt as a governed, auditable artifact**. Nobody can answer "why did *this* tenant see *exactly* this context for *that* request, and prove it byte-for-byte?"

ContextOS can. The flagship **Context Replay Debugger** delivers byte-exact replay of every deterministic context decision from a content-addressed, per-tenant-encrypted bundle. Memory and caching are necessary; **provable, governed, replayable context assembly is the moat.**

---

## What ContextOS Is (and Is Not)

ContextOS is **middleware** — a control plane plus a hot-path kernel — not an application and not a model. An app points its OpenAI SDK at the ContextOS gateway; ContextOS assembles the right context under a token budget, routes to the right backend, enforces tenant isolation, caches what it safely can, and records every decision for replay.

It is deliberately **not**:

| ContextOS is NOT | Because that is owned by | What ContextOS does instead |
| --- | --- | --- |
| an LLM / inference engine | vLLM, TGI, the model vendors | proxies and routes to them; never runs weights |
| a vector database | Postgres/pgvector, Qdrant | scores/MMRs over **≤512 pre-retrieved** candidates; **never builds or owns an index** |
| a training system | the fine-tuning stack | consolidates memory as an async, rate-limited, **cost-tracked** batch job — not a learning loop |
| an agent framework | LangGraph, CrewAI, AutoGen | treats agent-trace spans as **read-only** correlation; never schedules or re-executes steps |

These boundaries are load-bearing. The instant ContextOS builds an index, schedules a GPU, or re-executes an agent step, it inherits a competitor's entire surface area and loses the thing that makes it adoptable: it is the neutral substrate *underneath* all of them.

---

## The Wedge: One Window, Five Half-Solutions

The market has fragmented the context window along functional seams. Each incumbent is excellent at its slice and structurally blind to the joint problem:

| Slice | Incumbents | What they own | What they cannot do |
| --- | --- | --- | --- |
| Memory | mem0, Zep | store/retrieve user & session memory | enforce a token budget, prove isolation at pack time, or replay the assembly |
| Cache | GPTCache | semantic response caching | reason about per-tenant memory-grounded non-cacheability or share a tenant boundary with retrieval |
| Routing | LiteLLM, OpenRouter | model selection & failover | tie routing to the *assembled* prompt's tokenizer, residency policy, or budget ledger |
| Observability | Langfuse, Helicone | trace & cost dashboards | reconstruct a request **byte-exact** — they log *what happened*, not enough to *re-derive* it |
| Orchestration | LangChain, LlamaIndex | chains, agents, glue | give you a governed runtime boundary; they are libraries you compile *into* your app, inheriting your blast radius |

The seam nobody owns is the one that matters: **the context window is a single shared resource that must be jointly optimized.** Memory recency interacts with assembler ordering. Cache fingerprints depend on the system-prompt version and the stable-fact set that retrieval produced. Routing must run *before* final packing so the correct tokenizer enforces the hard reserve (C3). Compression must run *after* ACL/redaction so it never compresses data the tenant was never allowed to see (pipeline invariant). Solve these in five separate tools and the seams between them are exactly where token budgets blow, isolation leaks, and "why did the model see that?" becomes unanswerable. ContextOS owns the seams.

---

## Rejected Positionings (and Why Each Fails)

We explicitly considered — and rejected — shipping ContextOS as an extension of an existing tool:

- **ContextOS as a LangChain/LlamaIndex plugin.** Rejected. A library compiled into the application shares the application's process, trust boundary, and blast radius. Multi-tenant zero-trust isolation enforced by **Postgres FORCE ROW LEVEL SECURITY + an app RBAC firewall** (CI-gated by ≥10,000 hostile second-tenant property probes, target cross-tenant leakage = **0**) is impossible to guarantee when the "enforcement" is just imported Python the host app can monkey-patch. Isolation must be a *server boundary*, not a function call.

- **ContextOS as a LiteLLM feature.** Rejected. LiteLLM's center of gravity is the routing/proxy slice. Bolting replayable assembly, semantic memory, and per-tenant crypto-shred onto a router inverts the architecture: assembly and isolation become afterthoughts to dispatch, when in fact **routing is a downstream stage that depends on the assembled prompt** (it must know the packed token count to enforce the hard reserve, C3). The OS cannot be a plugin to one of its own syscalls.

- **ContextOS as a mem0 / GPTCache bolt-on.** Rejected. Memory and cache are *two stages* of one pipeline, not the pipeline. A memory bolt-on cannot enforce the budget knapsack; a cache bolt-on cannot know that a **memory-private-grounded response is non-cacheable** (C6) because it never participated in retrieval or ACL. Joint optimization requires a shared tenant boundary, a shared cost ledger, and a shared replay log — none of which a bolt-on can synthesize after the fact.

The through-line: each rejection collapses because the context window is a *joint* decision. Owning one slice and adapting the rest produces a system that is locally optimal and globally incoherent.

---

## Flagship: The Context Replay Debugger

The killer feature is **byte-exact replay of every deterministic context decision.** For any past request, ContextOS reconstructs — from a **content-addressed, per-tenant-encrypted bundle** — exactly what was retrieved, what was redacted, what was compressed, how candidates were scored and packed, which model was selected, and why. Every stage of the pipeline up to `backend.invoke` is **deterministic and asserted byte-equal** under one canonical `ReplayResult` schema (C7).

The honest contract matters: `backend.invoke` is non-deterministic, so byte-equality is asserted **only for recorded-output replay**; a `live_backend=True` replay yields a structured **diff**, not byte-equality. This is the difference between observability that *describes* and a debugger that *reproduces*. Langfuse tells you the request was slow and expensive; ContextOS hands you the exact context the model saw and lets you re-run the assembly, change one knob, and see the delta — deterministically, scoped to the tenant, with the bundle decryptable only under that tenant's key. No incumbent can do this because none of them own the whole decision; you cannot replay a pipeline you only observed one stage of.

---

## Headline Numbers (Canonical)

| Metric | Target |
| --- | --- |
| Context assembly (score + MMR + budget-knapsack over ≤512 candidates; excl. retrieval I/O & inference) | **< 50 ms p95** |
| Memory retrieval (embed + pgvector ANN ‖ BM25 + RRF + rescore) | **< 100 ms p95** (hot path ~40 ms) |
| Total ContextOS control overhead (everything around the model call; excl. inference) | **< 250 ms p95** |
| pgvector HNSW ANN probe (launch scale ≤5M vectors/tenant) | **18 ms p95** |
| Exact-hash cache lookup (Redis) | **< 1 ms p99** |
| Semantic-ANN cache lookup (incl. query embedding on miss) | **8–15 ms p95** |
| Query embedding (in-process CPU, BAAI/bge-small-en-v1.5, 384-dim) | **~6 ms p95** |
| Gateway throughput (stateless, hot path CPU-bound) | **5k–10k req/s per node** |
| Availability (gateway/control plane, ≥3 replicas across ≥3 AZ) | **99.9%** |
| Cross-tenant leakage (FORCE RLS + RBAC firewall; CI gate ≥10,000 hostile probes) | **0** |
| Semantic cache hit-ratio (coarse fingerprint, realistic mixed workload) | **25–45%** |
| Token-cost savings (caching 15–30% + routing downgrade 20–40% of model spend + compression 10–25%) | **40–65% combined** |
| Compression (NLI-guarded, long blocks) | **2–4× reduction at ≥98% fact retention** |

Note: cache, retrieval, and assembly **overlap**; the < 250 ms overhead is a **critical-path p95, not a naive sum** of the stage table. Model inference is excluded from every ContextOS budget.

---

## Target Users

- **Enterprise platform teams** — need provable per-tenant isolation, residency-aware routing, billing-grade cost records, and replay for audit/incident review; ContextOS is the governed substrate under their internal LLM platform.
- **AI startups** — need 40–65% token-cost savings and a memory + cache + routing stack on day one without building or operating five vendors; ContextOS is a base-URL change, not a re-architecture.
- **Dev / platform teams** — need to debug "why did the model see *that*?" deterministically; the Replay Debugger turns context bugs from forensic guesswork into reproducible diffs.

---

## Why Now

Three forces converge in 2026. **First, context windows are huge and ruinously expensive** — a million-token window is a budget allocation problem every request, and unmanaged assembly is now the dominant controllable cost line. **Second, multi-tenant LLM products are mainstream**, which makes per-tenant isolation, residency, and RTBF crypto-shred (C11) compliance requirements, not nice-to-haves — and "we imported a library" is not a defensible isolation story to an auditor. **Third, the tooling has fragmented into exactly five strong-but-siloed slices**, which is the historical signature of a missing operating layer: when the periphery matures and the center is still improvised in every app, the substrate beneath the periphery becomes the highest-leverage open-source position. The context window got a memory layer, a cache, a router, and a dashboard. It never got an OS. ContextOS is that OS — and the Replay Debugger is the proof that it owns the whole decision, not a slice of it.
