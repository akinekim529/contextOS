# System Architecture

ContextOS is **middleware**, not a model and not a database. It sits on the wire between an application and any LLM backend and owns the five things every team currently re-implements badly: durable multi-tenant memory, context assembly under a hard token budget, semantic + exact caching, model routing, and replay-grade observability. This section defines the component topology, the end-to-end data flow with per-hop latency, the split between the synchronous hot path and the asynchronous background plane, the consistency model and its user-visible consequences, and the scope-boundary and pipeline-ordering invariants that keep ContextOS from drifting into being "an agent framework with a vector DB stapled on."

Everything here is constrained by one rule: **ContextOS adds < 250 ms p95 of control overhead around the model call** (model inference excluded). Every architectural choice below is justified against that number and names the alternative we rejected.

---

## 1. Architectural stance and rejected alternatives

| Decision | Choice | Rejected alternative | Why the alternative fails *for middleware* |
|---|---|---|---|
| Control-plane runtime | Python 3.11+ asyncio + uvloop | Go, Node | Python owns the LLM/embedding/eval ecosystem (tokenizers, BGE, NLI guards). uvloop closes most of the event-loop gap; the genuinely CPU-bound 5% (MMR/knapsack) is isolated behind the ADR-0001 Rust gate, not paid for by rewriting the whole control plane. |
| Hot-path kernel | **PROVISIONAL** Rust/PyO3 (ADR-0001 gate, C14) | "Rust everything now" | Premature. We ship pure-Python assembly; we only cross the FFI boundary if the assembler p95 breaches its threshold under the benchmark in §C14. Rewriting before measuring is how you ship a slow Rust binary. |
| Edge protocol | REST/JSON, OpenAI-compatible `/v1` | Bespoke SDK-only protocol | The entire value prop is "change your base URL." A proprietary wire format means a rewrite at every caller; OpenAI-compat means a one-line drop-in. |
| Inter-service transport | In-process at launch; internal **gRPC** once boundaries prove out | gRPC-from-day-one | Network hops you can't yet justify. In-process calls cost nanoseconds and zero serialization; we promote to gRPC only when a boundary needs independent scaling (the embedding service, C15, is the first). |
| Async plane | Redis Streams + custom asyncio consumer (doubles as **replay log**) | Kafka | Kafka is operationally heavy for a self-hostable OSS middleware and gives us nothing Redis Streams doesn't at our volume. Reusing Redis Streams as the durable replay log means one ordered, consumer-group-tracked event spine instead of two. |
| Relational + vector | Postgres 16 (FORCE RLS, `tenant_id` partition key) + **pgvector** HNSW co-located | Dedicated vector DB (Qdrant/Pinecone) from day one | A second datastore means a second isolation model, a second backup story, and a distributed transaction across "row deleted" vs "vector deleted." Co-location gives one RLS boundary and one crypto-shred scope (C11). Qdrant is the **escape hatch** beyond 5M vectors/tenant, not the default. |
| Embeddings | Self-hosted BGE (`BAAI/bge-small-en-v1.5`, 384-dim) via `EmbeddingProvider` | OpenAI/Cohere embedding API | A network call (50-200 ms + cost + a tenant-data egress) inside a < 100 ms retrieval budget is disqualifying. In-process CPU BGE is ~6 ms p95 and never leaves the tenant boundary. |

---

## 1b. In-pipeline algorithm & latency choices (rejected alternatives)

Section 1 justifies the *infrastructure* picks. This table justifies the *algorithms* that run inside the hot path itself. The Memory subsystem and Context Assembly sections own the full derivations (cross-referenced below); these are the one-line rejections so the architecture document does not assert a latency number without naming what it rejected.

| Pipeline stage | Algorithm / latency choice | Rejected alternative | Why the alternative fails *here* | Owned by |
|---|---|---|---|---|
| Fusion (Hop 3) | **RRF, `k = 60`** (rank-only fusion of vector + BM25) | (a) Weighted-score fusion `α·vec + β·bm25` | Cosine similarity (∼0–1, dense) and BM25 (unbounded, corpus-dependent) are **scale-incompatible**; any fixed `α/β` mis-weights as soon as corpus statistics shift. RRF consumes only **ranks**, so it is invariant to score scale. | Memory subsystem |
| Fusion (Hop 3) | **RRF, `k = 60`** | (b) Learned fusion (LTR / cross-encoder re-ranker on the default path) | Needs **per-tenant labelled relevance judgements we do not have**, and adds a model call that breaks the < 100 ms retrieval SLO. A cross-encoder stays **opt-in / out-of-band** (C13). | Memory subsystem |
| Diversity (Hop 6) | **MMR, `λ = 0.70`, cosine-dup cutoff `0.95`** | (a) Pure top-k by fused rank | Top-k packs **near-duplicate** candidates (same fact restated), wasting the token budget; MMR's relevance/diversity trade-off (`λ·rel − (1−λ)·max_sim`) drops redundant blocks. | Context Assembly |
| Diversity (Hop 6) | **MMR over ≤ 512 candidates** | (b) Clustering (e.g. k-means / agglomerative then pick centroids) | Clustering over ≤ 512 vectors is **O(n²) / iterative and too slow for the < 50 ms assembly SLO**; MMR is a single greedy O(n·k) pass on already-retrieved candidates. | Context Assembly |
| Budget packing (Hop 6) | **Greedy budget knapsack** (value-density order, hard token reserve) | Exact 0/1 DP knapsack | Exact DP is **O(n·W)** in token-budget units (W ≈ 10⁵ tokens) — far too slow for < 50 ms; the greedy value-density packer is within a bounded ratio of optimal and runs in O(n log n). | Context Assembly |
| Vector ANN (Hop 3) | **pgvector HNSW, `m = 16`, `ef_construction = 64`, query-time `ef_search = 64` → p95 = 18 ms** | IVFFlat (`lists`/`probes`) | At **≤ 5M vectors/tenant**, IVFFlat needs a high `probes` to match HNSW recall — which **pushes p95 above 18 ms and degrades recall@k at the p95 tail** — and its recall/latency drift under high-churn tenant data forces periodic `REINDEX`/list re-tuning. HNSW holds recall ≥ 0.95 at `ef_search = 64` while staying at the canonical **18 ms p95** under incremental inserts. Qdrant is the escape hatch beyond 5M (§1). | Memory subsystem |

> The latency figures in this table (RRF + rescore 6 ms, MMR/knapsack inside the 50 ms assembly, HNSW 18 ms) are the **same canonical numbers** as Section 4 and Section 9; this table only attaches the rejected alternative and the tuning constant (`k`, `λ`, dup cutoff, `m`/`ef_search`) to each.

---

## 2. Component inventory

| Component | Responsibility | Owns (authoritative state) | Stateful? |
|---|---|---|---|
| **Gateway** (FastAPI/Starlette on Uvicorn) | OpenAI-compatible `/v1` ingress; parse, auth, tenant resolve, SSE streaming, backpressure | Nothing durable (request-scoped context only) | **No** (stateless; ≥3 replicas / ≥3 AZ) |
| **Auth & Tenant Resolver** | API-key → `tenant_id` + principal; sets `SET LOCAL` for RLS | — (reads tenant config) | No |
| **RBAC Firewall** | `check(principal, resource, action)` — single authority for `read/write/delete/admin/route/cache_read`; namespace + model-allowlist + residency (C2, C10) | — (reads policy store) | No |
| **Cache Layer** | Two-tier: exact-hash (Redis) + semantic-ANN (pgvector/Qdrant); per-tenant namespaced (C5/C6) | Cache entries (derived, evictable) | **Yes** (Redis + vector tier) |
| **Memory Engine** | Returns ≤512 candidates with **raw per-modality scores only** (C1); embed + ANN ‖ BM25 + RRF fuse + rescore | Memory rows/vectors (Postgres+pgvector), working memory (Redis TTL) | **Yes** |
| **ACL / Redaction** | Repository-boundary hard namespace filter (fail-closed, C2); PII/secret redaction on candidate bodies | — (reads policy) | No |
| **Compressor** | 2-4× token reduction, ≥98% fact retention, NLI-guarded; **always after ACL/redaction** | — (transforms candidates) | No |
| **Context Assembler** | **Sole** final-ranking + budget-knapsack authority (C1); score+MMR+edge-placement over ≤512; enforces hard token reserve | — (pure function of inputs) | No |
| **Model Router** | Difficulty + utility + breaker; selects model **before** final packing (C3); hard filters fail-closed (C9), derives `allowed_backends` from RBAC `route` (C10) | — (reads static policy + health telemetry) | No |
| **Adapter Layer** | Backend-specific dispatch; OpenAI-compat → vendor wire; streaming + client-abort semantics (C8) | — (per-request connection) | No |
| **Async Consumer** (asyncio over Redis Streams) | Drains write-back + memory-consolidation + trace/cost events off the hot path; **durable replay log** | Stream offsets / consumer-group state | **Yes** (Redis Streams) |
| **Replay Bundler** | Content-addressed, per-tenant-encrypted replay bundles; emits the single `ReplayResult` schema (C7) | Replay bundles (content-addressed store) | **Yes** |
| **Embedding Service** | Self-hosted BGE inference; own K8s Deployment + KEDA (C15) | — (model weights, stateless inference) | No (its own Deployment) |
| **Control Plane Stores** | Tenant config, RBAC policy, **cost ledger** | Strongly-consistent Postgres rows | **Yes** (authoritative) |

---

## 3. Component & hot-path diagram

This mirrors `docs/diagrams/component-hotpath.txt`. Solid `──>` is the synchronous critical path; `╌╌>` is the asynchronous background plane.

```
                         ┌───────────────────────────────────────────────────────────────┐
   client app            │                        CONTEXTOS NODE (stateless)              │
  (OpenAI SDK,           │                                                                │
   base_url=ContextOS)   │   ┌──────────┐                                                 │
        │  POST /v1/...   │   │ GATEWAY  │  FastAPI/Starlette · Uvicorn · uvloop           │
        ├────────────────┼──>│ (OpenAI- │                                                 │
        │   SSE stream    │   │  compat) │                                                 │
        │<╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┼───┤          │                                                 │
        │                │   └────┬─────┘                                                 │
        │                │        │ 1. parse/auth/tenant + RLS SET LOCAL ........ 5 ms     │
        │                │   ┌────▼──────────┐  ┌──────────────┐                          │
        │                │   │ AUTH/TENANT   │─>│ RBAC FIREWALL│ check(principal,res,act) │
        │                │   └────┬──────────┘  └──────────────┘                          │
        │                │        │ 2. cache lookup ............................. 10 ms    │
        │                │   ┌────▼──────────┐         exact-hash (Redis <1ms p99)         │
        │                │   │  CACHE LAYER  │<───────┐semantic-ANN (8-15ms p95)           │
        │                │   └────┬──────────┘        │                                    │
        │                │        │ HIT ─────────────────────────────────> (stream out)   │
        │                │        │ MISS                                                   │
        │                │        │ 3. retrieve candidates (≤512) ............... 40 ms    │
        │                │   ┌────▼──────────┐   embed(reuse) · pgvector ANN 18ms          │
        │                │   │ MEMORY ENGINE │‖  BM25 12ms · RRF+rescore 6ms               │
        │                │   │ raw scores    │   ┌──────────────┐                          │
        │                │   │ only (C1)     │──>│ EMBED SVC     │ BGE 384d ~6ms (KEDA)     │
        │                │   └────┬──────────┘   └──────────────┘                          │
        │                │        │ 4. ACL / redaction (hard ns filter, fail-closed, C2)   │
        │                │   ┌────▼──────────┐                                             │
        │                │   │ ACL/REDACTION │                                             │
        │                │   └────┬──────────┘                                             │
        │                │        │ 5. compression (ALWAYS after ACL) 2-4×, ≥98% facts     │
        │                │   ┌────▼──────────┐                                             │
        │                │   │  COMPRESSOR   │ NLI-guarded                                 │
        │                │   └────┬──────────┘                                             │
        │                │        │ 6. assembly: score+MMR+knapsack+edge-place .. 50 ms    │
        │                │   ┌────▼──────────┐  SOLE final-rank + budget authority (C1)    │
        │                │   │ CTX ASSEMBLER │  (Rust kernel PROVISIONAL, ADR-0001)        │
        │                │   └────┬──────────┘                                             │
        │                │        │ 7. routing (before packing, C3) ............. 5 ms     │
        │                │   ┌────▼──────────┐  hard filters fail-CLOSED (C9)              │
        │                │   │ MODEL ROUTER  │  allowed_backends ← RBAC route (C10)        │
        │                │   └────┬──────────┘                                             │
        │                │        │ 8. adapter dispatch + first-token ........... 8 ms     │
        │                │   ┌────▼──────────┐                                             │
        │                │   │  ADAPTER      │──────────────────────> [ LLM BACKEND ]      │
        │                │   └────┬──────────┘   (model inference: NOT in budget)          │
        │                │        │ 9. stream tokens back to client                        │
        │                │        │ 10. async write-back enqueue ................ 2 ms     │
        │                │   ╔════▼══════════════════════════════════════════╗            │
        │                │   ║   REDIS STREAMS  (durable replay log)          ║            │
        │                │   ╚════╤═══════════╤═══════════╤══════════════════╝            │
        │                │   ╌╌╌╌▼╌╌╌    ╌╌╌▼╌╌╌╌    ╌╌╌▼╌╌╌╌╌╌╌                          │
        │                │  ASYNC CONSUMER  CONSOLIDATION  REPLAY BUNDLER                   │
        │                │  (write-back)    (rate-limited,  (content-addr,                  │
        │                │   memory upsert,  cost-tracked    per-tenant-enc)                │
        │                │   trace/cost)     BATCH job)      ReplayResult (C7)              │
        │                └───────────────────────────────────────────────────────────────┘
                                            │            │
                          ┌─────────────────▼──┐   ┌─────▼──────────────────────────┐
                          │ POSTGRES 16        │   │ CONTROL-PLANE STORES (strong)  │
                          │ FORCE RLS · tenant │   │ tenant config · RBAC policy ·  │
                          │ partition · pgvector│   │ COST LEDGER (fail-closed)      │
                          └────────────────────┘   └────────────────────────────────┘
```

---

## 4. End-to-end data flow (annotated with the canonical latency budget)

The numbers below are the **single source of truth** from the Section 9 latency table. They are reproduced here for the data-flow narrative; Section 9 owns them. **Critically, these stages overlap — the < 250 ms p95 is the critical-path p95, not the naive sum (which would be ~120 ms even summed).** Model inference is excluded from every ContextOS budget.

```
HOP  STAGE                                  p95     COUNTS TOWARD
───  ─────────────────────────────────────  ──────  ─────────────────────────────
 0   Edge: parse/auth/tenant + RLS SET LOCAL  5 ms   <250ms overhead
 1   Cache lookup (exact <1ms p99;            10 ms  <250ms overhead
       semantic miss = embed 6ms + ANN 4ms)
 2   Memory retrieval (embed reused;          40 ms  <100ms retrieval SLO
       pgvector ANN 18ms ‖ BM25 12ms;                (hot path typically ~40ms)
       RRF + rescore 6ms)
 3   ACL / redaction (hard ns filter)        — (within retrieval budget, in-proc)
 4   Compression (2-4×, ≥98% facts)          — (long blocks only; bounded, in-proc)
 5   Context assembly (score ≤512, MMR,      50 ms  <50ms ASSEMBLY SLO
       knapsack, edge-place)
 6   Model routing (difficulty+utility+brk)   5 ms   <250ms overhead
 7   Adapter dispatch + first-token h/s       8 ms   <250ms overhead
 8   Stream tokens (model inference excluded) —      (NOT a ContextOS budget)
 9   Async write-back enqueue                 2 ms   <250ms overhead (off hot path)
```

### Step-by-step (the PIPELINE ORDERING INVARIANT, made concrete)

> **PIPELINE ORDERING INVARIANT:** `auth/tenant → cache lookup → retrieve candidates → ACL/redaction → compression → assembly/packing → routing → adapter → stream → async write-back`. **Compression ALWAYS runs AFTER ACL/redaction** — you never compress a candidate the caller is not allowed to see, because compression is lossy and irreversible, and redacting *after* compression risks leaking a redacted fact into a summary.

1. **Auth / tenant resolve (5 ms).** Gateway validates the API key, resolves `tenant_id` + principal, and issues `SET LOCAL app.tenant_id = '<ulid>'` so Postgres FORCE RLS scopes *every* subsequent query. The RBAC firewall is now armed. **Missing/ambiguous namespace = deny (C2).**
2. **Cache lookup (10 ms).** Exact-hash tier in Redis first (< 1 ms p99). On miss, the semantic-ANN tier (8-15 ms p95, *includes* the ~6 ms query embedding). A **hit short-circuits the entire pipeline** and streams the cached completion. **Memory-private-grounded responses are flagged non-cacheable (C6)** — they never enter either tier. Coarse fingerprint = `hash(normalized-query-embedding-bucket + model_id + system_prompt_version + stable_fact_set_version)`.
3. **Retrieve candidates (40 ms).** Memory Engine query-embeds (reusing the embedding the cache tier already computed), runs **pgvector HNSW ANN (p95 = 18 ms)** in parallel with BM25 (12 ms), fuses with RRF, and rescores. It returns **≤ 512 candidates with raw per-modality scores only (C1)** — it does **not** rank or pack. This whole subsystem lives under the **< 100 ms retrieval SLO**.
4. **ACL / redaction.** At the *repository boundary*, the within-tenant namespace (project/agent/user) is applied as a **hard, fail-closed filter** evaluated with `tenant_id` (C2). Bodies are PII/secret-redacted. Shared-org namespace access is gated by an explicit `RBACPolicy` rule.
5. **Compression (after ACL).** Long candidate blocks are compressed **2-4× with ≥ 98% NLI-guarded fact retention**. Short candidates pass through untouched. This shrinks prompt tokens 10-25% before packing.
6. **Assembly / packing (50 ms).** The Context Assembler is the **sole final-ranking and budget-knapsack authority (C1)**: it applies the one weight vocabulary, runs MMR for diversity, solves the budget knapsack against the model's hard token reserve, and applies lost-in-the-middle edge-placement. Memory-decay recency (a Memory-Engine concern) is **orthogonal** to this ordering. This is the **< 50 ms p95 assembly SLO** and the candidate for the PROVISIONAL Rust kernel.
7. **Routing (5 ms).** The router selects the backend **before final packing (C3)** so the *correct tokenizer* enforces the hard reserve. If the model isn't knowable in time, pack against a conservative max-tokenization estimate + documented margin and **re-validate post-route (re-pack or fail-closed 413)**. Hard-policy filters (allowlist, residency, capability, budget) evaluate on **static policy and fail-closed (C9)**; `allowed_backends` derives from the single RBAC `route` check (C10).
8. **Adapter dispatch (8 ms).** The adapter translates the OpenAI-compatible request to the vendor wire format and opens the stream. **Model inference latency is the backend's, not ours.**
9. **Stream.** Tokens flow back over SSE. **Client-abort semantics (C8):** if the server reaches `finish_reason` → commit write-back; if the client TCP-closes before the server terminal event → discard. Partial-cost attribution is defined where the abort is detected.
10. **Async write-back enqueue (2 ms).** A single append to Redis Streams. **All durable work happens off the hot path** (next section).

---

## 5. Synchronous hot path vs. asynchronous background plane

The architecture is deliberately bimodal. The hot path does the *minimum* needed to produce a correct, budget-packed request and stream a response; everything that can be deferred is.

### What runs SYNCHRONOUSLY (on the critical path)

| Stage | Why it must be synchronous |
|---|---|
| Auth/tenant + RLS | Security boundary; nothing may proceed without it. |
| Cache lookup | A hit avoids all downstream cost — it's the cheapest win, must be inline. |
| Retrieve → ACL/redaction → compression → assembly | These produce the prompt; the request cannot be sent without them. |
| Routing → adapter → stream | The actual model call. |

### What runs ASYNCHRONOUSLY (background plane, via Redis Streams)

Redis Streams is **one ordered, durable, consumer-group-tracked event spine** that doubles as the **replay log**. Every hot-path request appends exactly one event (the 2 ms enqueue). The Async Consumer drains it:

| Background job | Trigger | Properties |
|---|---|---|
| **Memory write-back** | `finish_reason` reached (C8) | Upsert episodic/semantic memory + embeddings into Postgres+pgvector. Discarded if client aborted pre-terminal. |
| **Memory consolidation** | Rate-limited batch | **Async, rate-limited, COST-TRACKED batch job — NOT an agent loop.** Its inference cost enters the budget ledger. |
| **Trace records** | Sampled (C12) | **Best-effort, fail-open, tail-sampled 1-10% + force-keep errors and `cost > $0.05/req`.** |
| **Cost records** | Every billable event (C12) | **Billing-grade, fail-CLOSED durable outbox.** A trace may be dropped; a cost record may never be. |
| **Replay bundling** | Per request (or sampled by policy) | Content-addressed, per-tenant-encrypted bundle; emits the single `ReplayResult` schema (C7). |

Because the replay log *is* the write-back stream, the Context Replay Debugger replays **exactly what the system did** — there is no separate, drift-prone audit log to reconcile.

---

## 6. Consistency model and its user-visible consequences

ContextOS runs **two consistency regimes** deliberately, because tenant config and money demand strong consistency while memory tolerates (and benefits from) eventual consistency.

### 6.1 Strong consistency — control plane

| Data | Store | Guarantee |
|---|---|---|
| Tenant config | Postgres (control-plane) | Read-after-write; a config change is visible on the next request, globally. |
| RBAC policy | Postgres (control-plane) | Strongly consistent; the single authority for `route`/`cache_read`/etc. (C10). Hard filters evaluate on this **static** policy and fail-closed even if the health store is down (C9). |
| **Cost ledger** | Postgres + **fail-closed durable outbox** (C12) | Never lost. Billing-grade. Consolidation inference cost is debited here. |

**User-visible consequence:** a residency restriction, a model-allowlist change, or a budget cap takes effect immediately and is never silently bypassed. If the policy store is unreachable, the router falls back to a **safe-default pool that itself satisfies every hard filter** (residency is never bypassed, C9) — it fails *closed*, not open.

### 6.2 Eventual consistency — memory, with read-your-writes

Long-term/episodic/semantic memory (Postgres+pgvector) is written **asynchronously** after `finish_reason`. That write is eventually consistent: a fact learned in turn *N* may not be ANN-retrievable for tens of milliseconds.

The naive consequence — "the model forgets what it just said" — is **eliminated by session-sticky working memory in Redis (TTL)**. The working/short-term tier is written **synchronously** within the request and is read on the next turn of the same session *before* the eventual long-term ANN results land. This gives **read-your-writes within a session** without paying for synchronous vector indexing on the hot path.

| Tier | Store | Write timing | Consistency | Used for |
|---|---|---|---|---|
| Working / short-term | Redis (TTL) | **Synchronous** | Read-your-writes (session-sticky) | "What did we just say?" — same-session continuity |
| Long-term / episodic / semantic | Postgres 16 + pgvector | **Asynchronous** (write-back) | Eventual | Cross-session recall, semantic retrieval |

**User-visible consequences, stated plainly:**

- *Within a session:* full read-your-writes. The just-completed turn is immediately available next turn via Redis working memory.
- *Across sessions:* a fact written in one session becomes ANN-retrievable in another after the write-back drains (sub-second under normal load). This is acceptable because cross-session recall is not latency-critical and the alternative (synchronous HNSW insert on the hot path) would blow the < 100 ms retrieval budget.
- *On client abort before server terminal (C8):* the long-term write-back is **discarded** — the interaction is treated as if it never produced durable memory. Working-memory TTL entries expire harmlessly.

---

## 7. Scope-boundary invariants (C13) — stated explicitly

ContextOS **is not, and must not drift into:** an LLM, an inference engine, a vector DB, a training system, or an agent framework. These invariants are load-bearing and are re-asserted in every section that could violate them:

1. **In-process scoring/MMR over pre-retrieved candidates only (≤ 512).** ContextOS **never builds or owns an index.** The candidate hard cap into assembly is ≤ 512. We operate on what the retrieval step already produced.
2. **Any cross-encoder reranker is OPT-IN and out-of-band.** The default embedder is `BAAI/bge-small-en-v1.5` (384-dim); a cross-encoder is never on the default hot path.
3. **Agent-trace spans are READ-ONLY correlation.** ContextOS never schedules or re-executes agent steps — it correlates spans for observability only.
4. **Memory consolidation is an async, rate-limited, COST-TRACKED batch job — not an agent loop.** Its inference cost enters the budget ledger.
5. **GPU-aware routing is a telemetry READER.** It reads GPU/queue telemetry to inform routing optimization signals; it **never schedules GPUs.**

These invariants are *why* the latency budget closes: ContextOS does cheap, bounded, in-process work (≤ 512 candidates, ~6 ms embeds, < 50 ms assembly) and delegates everything heavy (indexing, inference, scheduling) to systems it does not own.

---

## 8. Where each guarantee is enforced (cross-section map)

| Guarantee | Enforced by | Section that owns the detail |
|---|---|---|
| Cross-tenant leakage = 0 | Postgres FORCE RLS + RBAC firewall; CI gate ≥ 10,000 hostile probes | Security / multi-tenancy |
| < 50 ms assembly p95 | Context Assembler (Rust kernel PROVISIONAL, ADR-0001) | Context Assembly + §C14 gate |
| < 100 ms retrieval p95 (~40 ms hot) | Memory Engine (pgvector ANN 18 ms ‖ BM25 + RRF) | Memory subsystem |
| < 250 ms control overhead p95 | Critical-path budget (overlapping stages) | **Section 9 (latency budget — authoritative)** |
| 0 cost-record loss | Fail-closed durable outbox (C12) | Observability / billing |
| Byte-exact deterministic replay | Single `ReplayResult` schema; deterministic stages = all ContextOS decisions, `backend.invoke` non-deterministic (C7) | Replay Debugger |

---

## 8b. Cross-cutting interface skeletons (shown, not asserted)

Three structures cross every hop and pin the load-bearing invariants (C1, C7, the security boundary). They are *shown* here so the topology is unambiguous; the owning sections (Security, Memory, Replay) carry the full definitions. These are interface skeletons, not the complete types.

**`SecurityContext`** — request-scoped, set immediately after auth/tenant resolve (Hop 0); the single object every downstream stage consults for isolation. It is what drives `SET LOCAL app.tenant_id` and the RBAC firewall. Mirrors the canonical model in §09-roadmap D0.5; full definition in §07-security-model.

```python
class SecurityContext(BaseModel):   # frozen=True, extra="forbid"
    tenant_id: str                 # ULID; FORCE-RLS partition key + crypto-shred scope (C11)
    principal: str                 # ULID of user/agent/service account; passed to check(principal, res, act)
    namespace: str                 # within-tenant scope (project/agent/user); HARD filter, missing/ambiguous = DENY (C2)
    roles: tuple[str, ...]         # feeds the single RBAC check() (C10)
    request_id: str                # ULID; correlation key across trace + replay

# allowed_backends is NOT stored here — it is DERIVED, fail-closed, from the one RBAC
# check(principal, resource=model, action="route") (C10). Carrying it on the context
# would create a second policy store that could drift; RoutePolicy.allowed_backends owns it.
def allowed_backends(ctx: SecurityContext, model: str) -> tuple[str, ...]:
    return rbac.route_policy(ctx.principal, ctx.roles).allowed_backends  # C9 fail-closed
```
Example: `SecurityContext(tenant_id="01J9...Z", principal="01J9...P", namespace="proj/acme-prod", roles=("billing-agent",), request_id="01J9...R")` → `allowed_backends(...) == ("openai:gpt-4o", "anthropic:claude-sonnet")`.

**`MemoryCandidate` (raw scores only, C1)** — what the **Memory Engine returns** (≤ 512 of these; the built type is `contextos.models.memory.MemoryCandidate`). It carries **per-modality raw scores and the RRF fusion score**, but **deliberately has NO `final_rank` and NO `final_score`** field: final ranking and budget packing are the **sole authority of the Context Assembler (C1)**. The Memory Engine cannot rank; the type makes that structurally impossible. Score fields are nullable — `None` marks a modality the hit did not appear in (dense-only vs sparse-only).

```python
class MemoryCandidate(BaseModel):
    memory_id: str                 # ULID of the memory row / chunk
    content: str                   # raw text, pre-compression (ACL-redacted Hop 4, compressed Hop 5)
    vector_score: float | None = None  # raw cosine in [0,1]; None if sparse-only hit (Hop 3)
    bm25_score: float | None = None    # raw lexical rank; None if dense-only hit (Hop 3)
    rrf_score: float = 0.0             # RRF(k=60) fusion of the two ranks above — internal ordering, NOT a final rank
    # NOTE: no final_rank / no final_score — assembler owns those (C1)
```
Example: `MemoryCandidate(memory_id="01J9...A", content="ACME renewed on 2026-03", vector_score=0.83, bm25_score=14.2, rrf_score=0.0312)`.

**`ReplayResult` (single schema, C7)** — emitted by the Replay Bundler; the **one** object across API + observability + the flagship debugger. The determinism check is `rendered_prompt_hash` equality; `backend.invoke` is **exempt** because it is non-deterministic (C7). Mirrors the canonical model in §09-roadmap D0.5; full definition in §06-killer-features.

```python
class ReplayResult(BaseModel):              # ONE schema across API + observability + flagship (C7)
    request_id: str                         # correlation key (matches SecurityContext.request_id)
    tenant_id: str                          # per-tenant-encrypted replay scope (C11)
    rendered_prompt_hash: str               # sha256 of the EXACT bytes re-assembled — the byte-exact check
    deterministic_stages: dict[str, str]    # stage_name -> content-addressed hash of its decision
    output_equal: bool | None               # asserted ONLY for recorded-output replay; None for live-backend diff (C7)
    backend_invoke_deterministic: bool = False  # backend.invoke is NON-deterministic, never asserted equal (C7)
```
Example: `ReplayResult(request_id="01J9...R", tenant_id="01J9...Z", rendered_prompt_hash="e3b0c4...", deterministic_stages={"retrieve": "cas:7a1...", "assemble": "cas:9f2..."}, output_equal=True, backend_invoke_deterministic=False)`.

---

## 9. Summary of the architectural contract

ContextOS is a **stateless gateway** (5k-10k req/s per node, ~0.7 vCPU per 1k req/s, 99.9% on ≥3 replicas across ≥3 AZ with PDB `minAvailable=2`) fronting a **strongly-consistent control plane** (config/policy/cost) and an **eventually-consistent memory plane** (Redis working tier for read-your-writes, Postgres+pgvector for durable recall), bound by a **strict pipeline ordering invariant** (compression always after ACL/redaction) and a set of **scope-boundary invariants** that keep it middleware. The hot path does the minimum to pack a budget-correct prompt and stream; **Redis Streams carries everything else as a durable replay log**, which is what makes byte-exact replay possible. The whole system is engineered to a single number: **< 250 ms p95 of added control overhead, model inference excluded.**
