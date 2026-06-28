# 08 — Non-Functional Requirements: Latency, Throughput, Availability, and Scaling Economics

This section is the **single authoritative owner** of the ContextOS per-stage latency budget. Every other section that references a latency number references *this* table; if any other document states a different figure for a stage owned here, this section wins and the other is wrong. The same holds for the throughput envelope, the availability target, the cache hit-ratio / cost-savings derivation, and the per-service resource footprint.

The governing principle: **ContextOS is middleware, and middleware that adds unbounded tail latency to the model call is worse than no middleware at all.** Every NFR below is expressed as a *control-plane overhead* budget — the time ContextOS adds *around* the backend `invoke`, never including model inference, which is non-deterministic, provider-owned, and explicitly excluded from all ContextOS budgets (C7: `backend.invoke` is the one non-deterministic stage).

---

## 8.1 The Authoritative Per-Stage Latency Budget

ContextOS makes three distinct latency promises. They are nested, not additive, and confusing them is the most common error in middleware design. We state all three precisely and then derive them from one table.

| SLO | Bound (p95) | What it covers | What it excludes |
|---|---|---|---|
| **Context assembly** | **< 50 ms** | score + MMR + budget-knapsack + edge-placement over ≤ 512 pre-retrieved candidates | retrieval I/O, model inference, embedding |
| **Memory retrieval** | **< 100 ms** | query-embed + pgvector ANN ‖ BM25 + RRF fuse + rescore (hot path ~40 ms) | assembly, routing, model inference |
| **Total control overhead** | **< 250 ms** | everything ContextOS adds around the model call (critical path, not naive sum) | model inference only |

### 8.1.1 The table (canonical)

This is the one true latency budget. Stage p95s are *steady-state, warm-cache, launch scale* (≤ 5M vectors/tenant). Each row is annotated with whether it counts toward the `< 50 ms` assembly SLO and the `< 250 ms` overhead SLO.

| Stage | p95 | in <50ms assembly | in <250ms overhead |
|---|---|---|---|
| Edge: parse/auth/tenant resolve + RLS `SET LOCAL` | 5 ms | no | yes |
| Cache lookup (exact < 1 ms; semantic miss adds embed 6 ms + ANN 4 ms) | 10 ms | no | yes |
| Memory retrieval (embed reused; pgvector ANN 18 ms ‖ BM25 12 ms; RRF + rescore 6 ms) | 40 ms | no | yes (counts toward < 100 ms retrieval SLO) |
| Context assembly (score ≤ 512, MMR, knapsack, edge-place) | 50 ms | **YES** | yes |
| Model routing (difficulty + utility + breaker) | 5 ms | no | yes |
| Adapter dispatch + first-token handshake (ContextOS side) | 8 ms | no | yes |
| Async write-back enqueue (work runs off the hot path) | 2 ms | no | yes |

**Note:** cache lookup, retrieval, and assembly *overlap* on the critical path (see §8.1.3); `< 250 ms` is the critical-path p95, **not** a naive column sum. Model inference is excluded from every ContextOS budget.

### 8.1.2 Why assembly is a *subset*, and why retrieval has its own SLO

The `< 50 ms` assembly figure measures exactly one row — Context Assembly — because that row is the only stage doing *pure CPU work over already-materialized candidates*. Per the scope-boundary invariant (C13), the assembler scores, runs MMR, solves the budget knapsack, and places edges over **≤ 512 candidates** that retrieval already handed it. It performs **zero** I/O. That isolation is what makes a 50 ms p95 enforceable and meaningful: it is a pure-compute SLO an operator can profile, flame-graph, and regress-test deterministically.

Retrieval gets its own `< 100 ms` SLO because it is I/O-bound (pgvector ANN, BM25 scan, optional rescore) and therefore subject to a different failure surface (storage tail, connection pool saturation) than assembly's CPU surface. Folding them into one number would hide which subsystem regressed. The retrieval table row shows **40 ms** typical hot-path — well inside the 100 ms SLO, with the 60 ms of headroom absorbing cold HNSW pages, BM25 worst-case scans, and lock contention under load.

**Ownership boundary (C1):** retrieval returns candidates with *raw per-modality scores only*; the assembler is the *sole* final-ranking and budget-packing authority. The 50 ms assembly budget therefore includes the cost of *re-normalizing* those raw scores into the single assembler weight vocabulary — it does not trust upstream ordering. Memory-decay recency is an input feature to retrieval scoring; lost-in-the-middle edge placement is an assembler concern. They are orthogonal and never double-counted in the budget.

### 8.1.3 Deriving `< 250 ms` from the table (the critical path, not the sum)

Naive column sum: `5 + 10 + 40 + 50 + 5 + 8 + 2 = 120 ms`. That already sits comfortably under 250 ms — but it is the *wrong* number, because it assumes strict serialization. The real critical path exploits two overlaps:

```
                t=0
Edge auth/tenant  ├─5ms─┤
                        │
Cache lookup            ├──10ms──┤        (exact tier returns <1ms; full hit short-circuits everything below)
                        │
Query embedding         ├6ms┤             (computed ONCE, reused by both cache-semantic and retrieval — C3/C6)
                              │
Retrieval (ANN‖BM25)          ├────40ms────┤   ANN(18) ‖ BM25(12) run concurrently; RRF+rescore(6) joins
                                            │
Assembly                                    ├──────50ms──────┤
                                                              │
Routing                                                       ├5ms┤   (router runs BEFORE final packing — C3)
                                                                   │
Adapter handshake                                                  ├8ms┤
                                                                       │
Write-back enqueue (OFF critical path) ............................... ├2ms┤ (fire-and-forget, returns immediately)
```

**Critical-path p95** = edge(5) + cache-miss-embed(6) + retrieval(40) + assembly(50) + routing(5) + adapter(8) = **114 ms**, with the embedding cost shared (not paid twice) and write-back enqueue off-path. The remaining **136 ms** of the 250 ms budget is deliberate **tail headroom**: GC pauses, connection-pool checkout under load, a cold HNSW segment, RLS planner re-evaluation, and the C3 *re-validation after routing* (re-pack against the routed model's true tokenizer, or fail-closed 413). We budget overhead at `< 250 ms` rather than `< 150 ms` precisely so that p99/p99.9 events stay inside the SLO rather than blowing it.

**Rejected alternative — publishing the 120 ms sum as the SLO.** Tempting (it looks tighter, more impressive), rejected because it leaves *zero* tail headroom; the first GC pause or cold cache page would push p99 over the published bound and the SLO would be a lie under real load. A budget you cannot hold at p99 is marketing, not engineering. We publish the number we can defend at the tail.

**Rejected alternative — a single fused `< 250 ms` SLO with no sub-budgets.** Rejected because when overhead regresses you must know *which stage*. The nested SLOs (`50 ms` assembly ⊂ `100 ms` retrieval-class ⊂ `250 ms` overhead) give three independent regression alarms, each mapping to a different on-call runbook.

### 8.1.4 Stage latency provenance (why each number, what was rejected)

| Stage | Number | Why this, not the alternative |
|---|---|---|
| Edge auth/tenant + RLS | 5 ms | API-key/JWT verify + `SET LOCAL app.tenant_id` is a single round-trip-free planner hint. **Rejected:** per-request `CREATE ROLE`/`SET ROLE` (adds catalog lookups, ~ms each, and pollutes the role cache). FORCE RLS + session GUC is cheaper and equally fail-closed. |
| Cache lookup | 10 ms | Exact-hash tier is Redis `GET` at **< 1 ms p99**; semantic-ANN tier costs embed(6) + ANN(4) only on exact-miss, **8–15 ms p95** (C5/C6). **Rejected:** semantic-only cache (pays embed on every lookup, kills the < 1 ms fast path for repeated identical prompts). Two-tier keeps the sub-millisecond win for the exact tier and never claims it for the semantic tier. |
| Memory retrieval | 40 ms | pgvector **HNSW ANN p95 = 18 ms** (single source of truth ≤ 5M vectors/tenant) ‖ BM25 12 ms, RRF + rescore 6 ms, embed reused. **Rejected:** writing 15/25/40 ms for the ANN probe — those are different operating points; 18 ms is canonical at launch scale, with a **≤ 25 ms** Qdrant cutover holding beyond 5M vectors/tenant. |
| Context assembly | 50 ms | Pure CPU: score ≤ 512, MMR, 0/1 knapsack with a token-budget capacity, edge placement. **Rejected:** an LP/ILP exact knapsack solver (super-linear, blows the 50 ms budget at 512 items); we use a greedy density-ordered knapsack with an MMR diversity pass — within the FPTAS error tolerance, deterministic, and replay-stable. |
| Model routing | 5 ms | In-memory difficulty classifier + utility scorer + circuit-breaker read. Router runs **before** final packing (C3) so the chosen tokenizer enforces the hard reserve. **Rejected:** an external model-router microservice call (adds a network hop ≥ 8 ms to a 5 ms decision). In-process at launch (REST/gRPC only once boundaries prove out). |
| Adapter dispatch | 8 ms | Connection-pooled HTTP handshake to backend + first-token setup on the ContextOS side. **Rejected:** new TLS handshake per request (adds 1 RTT, ~tens of ms). Persistent pooled connections amortize it. |
| Write-back enqueue | 2 ms | A single Redis Streams `XADD` (the async plane doubles as the replay log). Off the critical path — the response returns before consolidation/trace/cost work runs. **Rejected:** synchronous Postgres write of memory + trace + cost on the hot path (this is precisely the write-amplification bottleneck of §8.5; doing it inline would add ~40–80 ms and serialize the response on durable I/O). |

---

## 8.2 Throughput: 5k–10k req/s per node

**Target: 5,000–10,000 req/s per gateway node**, stateless, horizontally scalable to N nodes behind an L4/L7 load balancer.

### 8.2.1 The assumptions and the math

The gateway is **stateless** and **hot-path CPU-bound** — the dominant cost is the assembler's score/MMR/knapsack over ≤ 512 candidates, not I/O (retrieval, cache, and backend calls are awaited asynchronously under `asyncio` + `uvloop`, so a single core multiplexes thousands of in-flight requests while they wait on I/O).

Sizing rule of thumb (canonical): **~0.7 vCPU per 1,000 req/s** of proxy + assembly work.

```
node_capacity_reqs = available_vCPU / 0.7 * 1000

  8 vCPU node:   8 / 0.7 * 1000  ≈  11,400 req/s  (theoretical)  → publish  ~10,000 req/s (headroom)
  4 vCPU node:   4 / 0.7 * 1000  ≈   5,700 req/s  (theoretical)  → publish  ~ 5,000 req/s (headroom)
```

We publish **5k–10k req/s/node** spanning a 4-to-8 vCPU node range, each with ~12–14% headroom shaved off theoretical to protect the p95 latency SLO under burst. The CPU floor is the assembler: at 10k req/s a node runs ~10k score/MMR/knapsack passes per second, which at 50 ms p95 each requires deep concurrency — sustained by `uvloop` event-loop multiplexing plus a bounded thread pool for the CPU-heavy knapsack inner loop (released to the loop between candidates to avoid head-of-line blocking).

### 8.2.2 Why CPU-bound, and what we rejected

| Decision | Rationale | Rejected alternative |
|---|---|---|
| `asyncio` + `uvloop` control plane | I/O concurrency (retrieval, cache, backend) overlaps cheaply; one process saturates many cores' worth of in-flight requests. | Thread-per-request (GIL contention + memory per thread caps concurrency far below 10k). |
| Stateless gateway | Any node serves any tenant's request; scale = add replicas. | Sticky/session-pinned nodes (rebalancing storms, hot tenants pin to one node). |
| Assembler as the CPU floor | It is the only unavoidable per-request compute; everything else is awaited I/O. | Pushing assembly to a separate service (adds a network hop inside the 50 ms budget — fatal). |
| Rust/PyO3 kernel **PROVISIONAL** (ADR-0001, C14) | If the Python assembler's p95 breaches threshold at target req/s, the score/MMR/knapsack kernel moves to Rust behind PyO3. | Rust-first from day one (premature; we ship Python and gate the rewrite on a measured benchmark — see §8.2.3). The Rust `ContextAssembler` interface is marked PROVISIONAL until the gate fires. |

### 8.2.3 The Rust-kernel gate (C14)

The gate is a *measured* trigger, not a vibe. **Benchmark definition:**

- **Workload:** candidate-count distribution skewed toward the cap — p50 = 200, p95 = 512, drawn from production retrieval traces; **384-dim** embeddings (BAAI/bge-small-en-v1.5).
- **Target load:** sustained **8,000 req/s per 8-vCPU node** (the upper publish band).
- **Trigger:** if the **Python assembler p95 exceeds 50 ms with less than 20% headroom** (i.e., p95 ≥ 40 ms) at that load, the Rust/PyO3 kernel rewrite is greenlit.
- **Owner:** the Kernel/Hot-Path team lead owns ADR-0001 and the benchmark harness; the gate is re-evaluated every release on the production candidate-count distribution.

Until the gate fires, the Python assembler is the shipping implementation and the Rust `ContextAssembler` interface stays PROVISIONAL.

---

## 8.3 Availability: 99.9% with Error-Budget Math

**Target: 99.9% (three nines) for the gateway / control plane.**

### 8.3.1 Error-budget math

```
99.9% availability  →  0.1% error budget

  per 30-day month:   0.001 * 43,200 min  =  43.2 min/month  of allowed downtime
  per 365-day year:   0.001 * 525,600 min =  525.6 min/year ≈ 8.76 h/year
  per week:           0.001 * 10,080 min  ≈  10.1 min/week
```

43.2 minutes/month is the spend allowance. Budget policy: a single rolling deploy that drains and replaces a replica must complete inside the PodDisruptionBudget without dropping below `minAvailable = 2` — so a routine deploy spends **zero** error budget. The budget is reserved for genuine incidents (AZ loss, Postgres failover, bad release rollback).

### 8.3.2 HA topology

| Property | Value | Why |
|---|---|---|
| Stateless replicas | **≥ 3** | Tolerate one replica loss + one in-flight deploy simultaneously. |
| Spread | **≥ 3 Availability Zones** | One full AZ outage leaves ≥ 2 AZs serving. |
| PodDisruptionBudget | **minAvailable = 2** | Voluntary disruptions (node drain, deploy) can never drop below 2 live replicas. |
| Load balancer | L7, health-checked, fail-out on readiness probe | A wedged replica is ejected before it spends budget. |

**Rejected alternative — 99.99% (four nines) gateway SLO.** Four nines = 4.32 min/month, which forces multi-region active-active and synchronous cross-region quorum for the control plane. That is unjustified for *middleware* whose backends (the LLM providers) themselves publish ~99.9% — promising more availability than the thing you proxy is a promise you cannot keep. We match the dependency floor and put the engineering into *correctness* (zero cross-tenant leakage, byte-exact replay) where middleware actually differentiates.

**Stateful dependencies and their own posture.** The 99.9% gateway SLO presumes Postgres 16 (primary + sync standby, automatic failover ≤ ~30 s) and Redis (replicated, AOF) each meet or exceed their own availability targets. A Postgres failover spends ~30 s of error budget per event; at ≤ 1 unplanned failover/month this consumes < 1.2% of the monthly budget, leaving ample margin for everything else.

---

## 8.4 Horizontal Scaling to N Tenants / M Sessions

| Axis | Scaling mechanism | Bound |
|---|---|---|
| **Gateway throughput** | Add stateless replicas (no shared state). | Linear to LB capacity. |
| **N tenants** | `tenant_id` is a non-null partition key on *every* row/object/key (C2). Postgres native partitioning by `tenant_id`; Redis + cache namespaced per tenant. | Partition count, not data volume, is the ceiling. |
| **M sessions/tenant** | Working/short-term memory in Redis with TTL; long-term/episodic/semantic in Postgres + pgvector. Session state is ephemeral and evicts. | Redis memory (bounded by TTL). |
| **Vector volume** | pgvector HNSW co-located in Postgres up to **5M vectors/tenant** (ANN p95 18 ms); **Qdrant escape hatch** beyond, holding ≤ 25 ms. | Per-tenant 5M-vector cutover line. |

Tenant isolation scales without a per-tenant database: FORCE ROW LEVEL SECURITY + the app RBAC firewall guarantee **0 cross-tenant leakage**, validated by a CI hard gate of **≥ 10,000 hostile second-tenant property probes**. Within-tenant namespace (project/agent/user) is a **hard, fail-closed filter at the repository boundary** keyed on `tenant_id`; missing/ambiguous namespace = deny (C2).

---

## 8.5 The First Bottleneck: Postgres Write/IO at 10k req/s

At 10k req/s the gateway is fine (it is CPU-bound and scales by replicas). The **first wall is Postgres write I/O**, driven by **trace + cost write-amplification**: a single user request can fan out into *multiple* durable writes — one or more memory-consolidation upserts, an agent-trace correlation span, and a billing-grade cost record. Naively, **1 req → 3–5 writes**, so 10k req/s → 30k–50k write ops/s hitting one primary. Postgres WAL fsync and index maintenance become the ceiling long before CPU does.

### 8.5.1 How we remove it (the four levers — and C12 governs trace vs cost)

```
Hot-path request  ──XADD──▶  Redis Streams (replay log + async plane)
                                     │
                                     ├──▶ [TRACE consumer]  best-effort, fail-OPEN, SAMPLED
                                     │       tail 1–10% + force-keep(errors, cost>$0.05/req)
                                     │       → BATCHED COPY into time-partitioned trace tables
                                     │
                                     ├──▶ [COST consumer]  billing-grade, fail-CLOSED
                                     │       durable OUTBOX → exactly-once into cost ledger
                                     │       (NEVER dropped, NEVER sampled)
                                     │
                                     └──▶ [MEMORY consumer]  consolidation: async,
                                             rate-limited, COST-TRACKED batch (inference cost
                                             enters the budget ledger) — not an agent loop
```

| Lever | What it does | C12 alignment |
|---|---|---|
| **Async batched trace ingest** | Traces never touch the hot path; the Streams consumer batches via `COPY` (10–100× cheaper than per-row `INSERT`). | Best-effort, **fail-open**, **sampled**: tail 1–10% + force-keep on errors and cost > $0.05/req. Dropping a trace under pressure is acceptable. |
| **Fail-closed cost outbox** | Cost records go through a durable transactional outbox with exactly-once delivery to the ledger. | Billing-grade, **fail-closed**, durable. A cost record is *never* sampled or dropped — losing one is revenue loss / a billing dispute. |
| **Time-partitioning** | Trace and cost tables partitioned by time (and `tenant_id`); writes hit only the current partition; old partitions detach/archive cheaply. | Bounds index size so write cost stays flat as history grows. |
| **Read replicas** | Analytics, dashboards, and replay-bundle reads served from replicas. | Removes read contention from the write primary. |

The **asymmetry is the whole point of C12**: traces are *observability* (lossy-OK, optimize for cheap volume), cost records are *billing* (lossless-required, optimize for durability). Treating them identically is the bug — either you over-engineer trace durability (and lose throughput) or under-engineer cost durability (and lose money). We split the write paths.

With these four levers, the durable write rate the primary sees drops from ~30–50k ops/s to ~**hundreds of batched `COPY` operations/s** (each carrying thousands of rows) for traces, plus a bounded outbox drain for costs — comfortably within a single Postgres 16 primary's WAL budget at launch scale, with read replicas absorbing all query load.

---

## 8.6 Cache Hit-Ratio and Token-Cost Savings

### 8.6.1 Hit-ratio: 25–45% (coarse fingerprint policy, C6)

```
COARSE cache signature = hash(
    normalized_query_embedding_bucket
  + model_id
  + system_prompt_version
  + stable_fact_set_version
)
```

The fingerprint is deliberately **coarse**: it buckets the normalized query embedding (so near-duplicate prompts collide into a hit) and keys on model + system-prompt version + stable-fact-set version. **Memory-private-grounded responses are flagged non-cacheable** — a response grounded in one user's private memory must never be served to another, even within a tenant.

Re-derived hit-ratio on a realistic mixed workload: **25–45%**. We do not claim higher because the coarse policy trades some precision for recall and the non-cacheable carve-out removes the most-personalized traffic from the cacheable pool. An **offline cache-correctness eval harness** (does a coarse-bucket hit return a semantically-acceptable response?) is a roadmap item — until it ships, the coarse bucket width is set conservatively to favor false-misses over false-hits.

### 8.6.2 Token-cost savings: 40–65% combined

The three savings levers are **multiplicative on the residual**, not additive on the gross — which is why the combined band is 40–65% and not the naive sum of the maxima.

| Lever | Savings | Mechanism |
|---|---|---|
| **Caching** | **15–30%** of model spend | Exact + semantic cache hits avoid the backend call entirely (25–45% hit-ratio, but cacheable traffic < 100% of spend → 15–30% net). |
| **Model-routing downgrade** | **20–40%** of model spend | Easy queries (difficulty classifier) routed to a cheaper model. Router fails *closed* on hard-policy filters (C9) and *open* to static ranking on optimization signals only. |
| **Compression** | **10–25%** prompt-token reduction | 2–4× token reduction on long blocks with **≥ 98% fact retention (NLI-guarded)**. Runs **after ACL/redaction** (pipeline invariant) so it never compresses redacted-away content back in. |

**Combined derivation (multiplicative on residual):**

```
remaining_spend = 1
remaining_spend *= (1 - cache_savings)        # e.g. 0.78  (22% cached)
remaining_spend *= (1 - routing_savings)      # e.g. 0.70  (30% routed cheaper)
remaining_spend *= (1 - compression_savings)  # e.g. 0.82  (18% fewer prompt tokens)

low  end:  1 - (1-0.15)(1-0.20)(1-0.10) = 1 - 0.612 = 0.388 ≈ 39%  →  publish floor 40%
high end:  1 - (1-0.30)(1-0.40)(1-0.25) = 1 - 0.315 = 0.685 ≈ 69%  →  publish ceiling 65% (conservative)
```

We publish **40–65%** — flooring the low end slightly up and the high end slightly down from the arithmetic because savings overlap (a cached query is also a routed/compressed query that never ran, so the levers are not fully independent). **Rejected alternative — additive 15+40+25 = 80%.** That double-counts: a request eliminated by caching cannot *also* save routing and compression dollars, because it never reached routing or compression. The multiplicative-on-residual model is the honest one.

---

## 8.7 Infrastructure Cost Envelope and Per-Service Footprint

### 8.7.1 Per-service CPU/RAM footprint

This table is the authoritative resource model for capacity planning and Helm `resources:` defaults.

| Service | Stateful? | vCPU (req / limit) | RAM (req / limit) | Replicas (launch) | Scaling signal |
|---|---|---|---|---|---|
| **Gateway / Control plane** | No (stateless) | 4 / 8 | 2 Gi / 4 Gi | ≥ 3 across ≥ 3 AZ | CPU (hot-path bound); ~0.7 vCPU / 1k req/s |
| **Embedding service** | No (stateless; model weights in RAM) | 2 / 4 | 4 Gi / 6 Gi | ≥ 2 | KEDA on embed-queue depth (C15) |
| **Async consumers** (trace / cost / memory) | No (consumes Redis Streams) | 1 / 2 | 1 Gi / 2 Gi | 2–3 | Redis Streams lag |
| **Postgres 16 + pgvector** | **YES** | 8 / 16 | 32 Gi / 64 Gi | 1 primary + 1 sync standby + N read replicas | Write IOPS / WAL; partition + replica |
| **Redis** (cache exact tier + Streams + working memory) | **YES** | 2 / 4 | 8 Gi / 16 Gi | replicated (AOF) | Memory pressure / eviction |
| **Qdrant** (escape hatch, > 5M vectors/tenant) | **YES** | 4 / 8 | 16 Gi / 32 Gi | conditional | Vector count > 5M/tenant |

**Stateful services: Postgres, Redis, Qdrant.** Everything else (gateway, embedding service, async consumers) is **stateless** and scales by adding replicas. The stateful tier is where availability engineering (sync standby, AOF, failover) and the §8.5 write-bottleneck work concentrate. Packaging is **uv + Hatchling, distroless images, Helm charts** — distroless keeps the attack surface and image size minimal for the stateless tier that scales horizontally.

### 8.7.2 Cost envelope and net economics

The control-plane infra cost is a small, *fixed* tax that pays for a *variable* model-spend reduction:

```
Per 1k req/s of sustained load:
  gateway:    ~0.7 vCPU/1k req/s  →  budget for 1 small node
  embedding:  ~6 ms p95/embed, CPU-only (no GPU) → fractional node
  Postgres/Redis: shared stateful tier, amortized across all tenants

Net economics:
  ContextOS infra cost  ≈  single-digit % of the model bill it sits in front of
  Token-cost savings    =  40–65% of that model bill (§8.6)
  →  Net: ContextOS PAYS FOR ITSELF many times over once model spend is non-trivial.
```

The embedding service is deliberately **CPU-only** (BAAI/bge-small-en-v1.5, 384-dim, ~6 ms p95) — **rejected alternative: GPU-hosted embeddings**, which would 10× the per-embed infra cost for a 384-dim model that runs in single-digit ms on CPU. GPU-aware routing in ContextOS is a *telemetry reader*, never a GPU scheduler (scope invariant); ContextOS owns no GPUs.

---

## 8.8 Embedding Service: Availability, Cold-Start, and Bounded Degraded Recall (C15)

The embedding service is its **own Kubernetes Deployment**, not an in-process library, so it scales and fails independently of the gateway.

| Property | Target | Rationale |
|---|---|---|
| **Availability** | **99.9%** (≥ 2 replicas, KEDA-scaled on embed-queue depth) | Matches the gateway floor; embedding is on the critical path for cache-semantic + retrieval. |
| **Cold-start ceiling** | **≤ 10 s** to ready (model weights pre-baked into the distroless image, loaded at boot; readiness probe gates traffic) | A scale-from-zero or pod-replacement event must not stall the hot path beyond 10 s; KEDA keeps ≥ 2 warm so cold-start affects *scale-up*, never *steady-state*. |
| **Warm p95** | **~6 ms** per embed (384-dim, in-process CPU) | Canonical query-embedding latency; reused across cache-semantic and retrieval so it is paid once per request (§8.1.3). |

### 8.8.1 Bounded degraded mode: BM25-only, recall loss ≤ 12%

If the embedding service is **unavailable or saturated**, retrieval **fails open to BM25-only** (lexical) retrieval rather than failing the request:

```python
async def retrieve(query, tenant_id, namespace):
    try:
        q_vec = await embedder.embed(query, timeout=EMBED_TIMEOUT_MS)
        ann   = pgvector_ann(q_vec, tenant_id, namespace)   # 18ms p95
        lex   = bm25(query, tenant_id, namespace)           # 12ms p95
        return rrf_fuse(ann, lex)                            # full recall
    except (EmbedUnavailable, EmbedTimeout):
        # DEGRADED: lexical only — bounded recall loss <= 12%
        metrics.incr("retrieval.degraded.bm25_only")
        return bm25(query, tenant_id, namespace)            # still serves, lower recall
```

The dense ANN leg drops; the **BM25 leg keeps serving** with a **bounded recall loss of ≤ 12%**. This is a deliberate availability-over-perfection trade: a 12%-worse-recall answer beats a 503. The bound is enforced by the C6/§8.6 fingerprint and retrieval design — lexical BM25 alone recovers ≥ 88% of the fused-mode recall on the evaluation workload.

**Streaming idempotency (C15):** a streaming request carrying an `Idempotency-Key` that has already reached a server terminal `finish_reason` returns the **materialized final response** with **zero second backend call** — the first run's recorded output is replayed, never re-invoked. This ties directly to the client-abort rule (C8): server `finish_reason` reached ⇒ write-back committed and the response materialized; a client TCP close *before* the server terminal event ⇒ discard, no materialized response, partial cost attributed to the partial-completion ledger entry.

---

## 8.9 NFR Summary Card

| Dimension | Target | Owner of detail |
|---|---|---|
| Context assembly p95 | < 50 ms (pure CPU, ≤ 512 candidates) | §8.1 (this section) |
| Memory retrieval p95 | < 100 ms (hot ~40 ms) | §8.1 (this section) |
| Total control overhead p95 | < 250 ms (critical path ~114 ms + tail headroom) | §8.1 (this section) |
| pgvector HNSW ANN p95 | 18 ms (≤ 5M vec/tenant; Qdrant ≤ 25 ms beyond) | §8.1 / §8.4 |
| Exact-hash cache p99 | < 1 ms (Redis); semantic 8–15 ms p95 | §8.1 / §8.6 |
| Throughput | 5k–10k req/s/node (~0.7 vCPU/1k req/s) | §8.2 |
| Availability | 99.9% (43.2 min/month budget; ≥ 3 replicas / ≥ 3 AZ; PDB minAvailable=2) | §8.3 |
| Cross-tenant leakage | 0 (FORCE RLS + RBAC firewall; ≥ 10k CI hostile probes) | §8.4 |
| First bottleneck | Postgres write I/O (trace+cost write-amplification) → async batched trace + fail-closed cost outbox + time-partitioning + read replicas | §8.5 |
| Cache hit-ratio | 25–45% (coarse fingerprint) | §8.6 |
| Token-cost savings | 40–65% (cache 15–30% × routing 20–40% × compression 10–25%) | §8.6 |
| Compression | 2–4× tokens, ≥ 98% fact retention (NLI-guarded) | §8.6 |
| Embedding availability / cold-start | 99.9% / ≤ 10 s; degraded BM25-only recall loss ≤ 12% | §8.8 |
| Stateful services | Postgres, Redis, Qdrant (all else stateless) | §8.7 |
