# 09 — Delivery Roadmap & Component-to-Milestone Map

This section is the build plan. It is deliberately opinionated about *sequencing*: ContextOS is middleware whose entire value proposition is **trust** (zero cross-tenant leakage, byte-exact replay, cost-tracked budgets), so the walking skeleton ships the trust-critical primitives — tenant isolation, the data models, and the leakage CI gate — *before* any of the "interesting" engineering (memory, assembly, routing). We refuse the common mistake of building the clever Context Assembler first and bolting on Row-Level Security later; security retrofits leak. Isolation is load-bearing structure, not a feature.

Three milestones, each with concrete deliverables, explicit deferrals, and the rationale that justifies the deferral. We close with (1) a full component-to-milestone matrix covering every subsystem named in the design, and (2) the Rust-kernel gate methodology (ADR-0001 / C14): the exact benchmark, threshold, headroom, and owner that decide whether the PROVISIONAL Rust hot-path kernel is ever built.

A milestone is **done** only when its exit criteria pass in CI. "It runs on my machine" is not a milestone.

---

## 0. Sequencing philosophy (why this order)

| Principle | Consequence for the roadmap |
| --- | --- |
| Isolation is structural, not additive | `tenant_id` partition key, `FORCE ROW LEVEL SECURITY`, and the >=10,000 hostile-probe leakage gate ship in **Week 1-2**, against an otherwise trivial service. Rejected alternative: "add multi-tenancy in Month 3" — by then every query, index, and cache key would assume single-tenant and the refactor would be a rewrite. |
| The data models are the contract | The 5 canonical Pydantic models (`SecurityContext`, `MemoryRecord`, `ContextBundle`, `RoutePolicy`, `ReplayResult`) are frozen in Week 1-2. Every later subsystem is an implementation behind these schemas. Rejected alternative: discover the schema by accretion — produces five incompatible dialects of "what a context is". |
| Prove the SLO before optimizing it | The pure-Python Context Assembler is built and **benchmarked against the < 50 ms p95 budget in Month 1**. Only a *measured* miss authorizes the Rust kernel (C14). Rejected alternative: write Rust first "because hot path" — premature, unmeasured, and an unjustified PyO3/cross-compile/distroless-glibc tax on every contributor. |
| The flagship is a wedge, not a finale | The Context Replay Debugger MVP (bit-exact rendered-prompt hash) lands in **Month 1**, not Month 3. Replay is the differentiator; it must exist early enough to drive the schema decisions (deterministic-stage capture) for everything downstream. Rejected alternative: ship replay last — by then the pipeline emits un-replayable side effects everywhere. |
| Defer anything whose absence is survivable in a degraded-but-correct mode | Qdrant, cross-encoder rerank, abstractive compression, service mesh, Kafka, multi-region — each has a named fallback that is *correct, just slower or coarser*. We ship the fallback first and the optimization on evidence. |

---

## 1. Milestone M0 — Walking Skeleton (Week 1-2)

**Theme: a single correct, isolated, replay-stub-emitting request path.** End-to-end one synchronous `POST /v1/chat` that authenticates a tenant, sets RLS, calls exactly one backend, and writes an audit trace — with the leakage CI gate green from the first merge.

### 1.1 Deliverables

#### D0.1 — Gateway: `POST /v1/chat` (sync only)
FastAPI/Starlette on Uvicorn, OpenAI-compatible request/response shape on `/v1`. Synchronous (no streaming) — the request blocks until the backend returns a full completion.

- **Rejected alternative:** Flask/WSGI — no native async, and our hot path is I/O-bound on backend + Postgres; a sync WSGI worker pool wastes the concurrency we get free from asyncio. Django REST — drags an ORM/admin/migration framework we do not want near the hot path.
- The handler executes the *first half* of the pipeline invariant only: `auth/tenant -> (cache stub: always miss) -> (retrieve stub: empty) -> (assembly stub: passthrough) -> routing (static, one backend) -> adapter -> response -> async write-back (sync stub)`. Cache/memory/assembly are **stubs that preserve the call signature** so Month 1 swaps implementations without touching the handler. The ordering invariant is encoded in the call sequence from day one even though most stages are no-ops.

#### D0.2 — One `BackendAdapter`: vLLM (OpenAI-compatible)
A single adapter implementing the `BackendAdapter` Protocol against a vLLM server's OpenAI-compatible endpoint.

- vLLM chosen because its `/v1/chat/completions` surface lets us reuse the OpenAI request/response shape end-to-end, so the adapter is a thin pass-through and the *interesting* logic stays in ContextOS where we can test it.
- **Rejected alternative:** wiring a hosted commercial API (OpenAI/Anthropic) as the first backend — couples M0 to a paid external dependency, network egress, and rate limits during the period we most need fast, free, deterministic local iteration. Those adapters arrive in the Month-3 adapter matrix.
- The adapter respects the **C8 client-abort contract** even in M0: if the server reaches `finish_reason` the response is committed to write-back; if the client TCP-closes before the server terminal event, the partial is discarded and partial-cost is attributed per C8 (tokens streamed-so-far × input-side price for the prompt already sent, recorded against the tenant's budget ledger as an aborted-request line item). Stating this in M0 prevents an abort-semantics retrofit later.

#### D0.3 — Tenant isolation: the non-negotiable core
This is the deliverable M0 exists for.

- `SecurityContext` resolved at the edge from the API key -> `(tenant_id, principal, namespace, roles)`. `tenant_id` is ULID, non-null.
- Postgres 16 with **`FORCE ROW LEVEL SECURITY`** on every tenant-scoped table. Every connection checked out from the pool runs, inside the request transaction:

```sql
-- Executed once per request, transaction-local, before any tenant query.
SET LOCAL app.tenant_id = $1;  -- $1 = ULID from SecurityContext, never client-supplied
```

```sql
-- Representative policy, applied to EVERY tenant-scoped table.
ALTER TABLE memory_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_record FORCE ROW LEVEL SECURITY;  -- FORCE => applies even to table owner
CREATE POLICY tenant_isolation ON memory_record
  USING      (tenant_id = current_setting('app.tenant_id')::text)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::text);
```

- `tenant_id` is also the **partition key** (declarative `PARTITION BY HASH (tenant_id)` on high-cardinality tables; list/hash chosen over range because tenant IDs are ULIDs with no natural range locality). Partitioning is structural here so the Month-1 vector and memory tables inherit it.
- `SET LOCAL` (transaction-scoped) chosen over `SET` (session-scoped) deliberately: a pooled connection must **not** carry one tenant's `app.tenant_id` into the next checkout. `SET LOCAL` auto-resets at transaction end; a leaked session GUC across a pooled connection is exactly the cross-tenant leakage we forbid. **Rejected alternative:** a per-tenant connection pool — multiplies connection count by tenant count and defeats pooling at any real tenant scale.
- **App RBAC firewall** (defense in depth): a repository-boundary check that the resolved `tenant_id` matches the row's `tenant_id` *in application code as well*, so a missing/misapplied RLS policy fails closed rather than silently leaking. C2 namespace rule is wired here in stub form: within-tenant namespace is a HARD, fail-closed filter; missing/ambiguous namespace = **deny**.

#### D0.4 — The leakage CI gate (day one, hard gate)
A property-based test harness that, per CI run, executes **>= 10,000 hostile second-tenant probes**: for each probe, tenant A writes a record, tenant B (different `SecurityContext`) attempts to read/enumerate/guess it via every API surface (direct ID, list, search-by-prefix, cache key, error-message oracle). **Canonical fact: cross-tenant leakage = 0.** Any single probe that returns A's data, or even confirms its existence, **fails the build**. This gate is a merge blocker from the first PR and never relaxes.

- **Rejected alternative:** a handful of hand-written isolation unit tests — they test the cases the author already thought of; the property-probe harness (Hypothesis-driven, randomized IDs/namespaces/sequences) finds the ones they did not. 10,000 is the canonical floor, scaled up nightly.

#### D0.5 — The 5 canonical data models (frozen schemas)
Pydantic v2 models, the contract for everything downstream. IDs = ULID, timestamps = RFC-3339 UTC, `tenant_id` non-null on every one.

```python
# All models: model_config = ConfigDict(frozen=True, extra="forbid")

class SecurityContext(BaseModel):
    tenant_id: str                  # ULID, partition key, non-null
    principal: str                  # ULID of user/agent/service account
    namespace: str                  # within-tenant: project/agent/user; HARD filter (C2)
    roles: tuple[str, ...]          # feeds RBAC check() (C10)
    request_id: str                 # ULID; correlation key across trace + replay
    issued_at: str                  # RFC-3339 UTC

class MemoryRecord(BaseModel):
    id: str                         # ULID
    tenant_id: str
    namespace: str
    tier: Literal["working", "short_term", "long_term", "episodic", "semantic"]
    content: str
    embedding_ref: str | None       # pointer to encrypted vector payload (C11 crypto-shred scope)
    raw_scores: dict[str, float]    # RAW per-modality scores ONLY (C1); assembler ranks
    decay_anchor: str               # RFC-3339 UTC; recency decay basis (orthogonal to assembler order, C1)
    created_at: str

class ContextBundle(BaseModel):
    id: str                         # ULID; content-addressed (sha256 of canonical serialization)
    tenant_id: str
    request_id: str
    rendered_prompt_hash: str       # sha256 of the EXACT bytes sent to the backend (replay anchor)
    selected: tuple[str, ...]       # ordered MemoryRecord IDs as packed (post-knapsack, edge-placed)
    token_budget: int
    tokens_used: int
    model_id: str                   # bound BEFORE final packing (C3 tokenizer truth)
    created_at: str

class RoutePolicy(BaseModel):
    tenant_id: str
    allowed_backends: tuple[str, ...]  # DERIVED from RBAC check(action="route") (C10) — no 2nd store
    residency: str                  # hard filter, fail-closed (C9)
    hard_reserve_tokens: int        # output reserve enforced with the bound model's tokenizer (C3)
    safe_default_pool: tuple[str, ...]  # MUST itself satisfy all hard filters (C9)

class ReplayResult(BaseModel):      # ONE schema across API + observability + flagship (C7)
    request_id: str
    tenant_id: str
    deterministic_stages: dict[str, str]  # stage_name -> content-addressed hash of its decision
    rendered_prompt_hash: str
    backend_invoke_deterministic: bool = False  # backend.invoke is NON-deterministic (C7)
    output_equal: bool | None       # asserted ONLY for recorded-output replay (C7)
    live_backend: bool              # if True => result is a DIFF, not byte-equality (C7)
    bundle_ref: str                 # content-addressed, per-tenant-encrypted replay bundle id
```

These schemas embed the consistency resolutions structurally: C1 (raw scores in memory, ranking in assembler), C3 (`model_id` on the bundle, bound pre-pack), C7 (the single ReplayResult), C9/C10 (route policy derived from RBAC, safe pool satisfies hard filters), C11 (encrypted embedding ref). Freezing them now means later sections cannot drift.

#### D0.6 — Sync trace stub
A synchronous, in-process trace writer emitting one structured span per request keyed by `request_id`, with `tenant_id`, stage timings, `model_id`, token counts, and cost. In M0 it writes synchronously to Postgres (an outbox table) — the **fail-closed billing-grade cost path (C12)** is correct from day one even though the **best-effort sampled trace path (C12)** and OTel export arrive in Month 3. We separate the two write paths in the schema now so Month 3 only changes *transport*, not *semantics*.

#### D0.7 — Packaging & deploy baseline
- **uv + Hatchling** for dependency resolution and build. Rejected: Poetry — slower resolver, and uv's lockfile + `uv pip` speed matters in CI and distroless image builds.
- **Distroless** runtime image (no shell, no package manager) — minimal attack surface for middleware that holds per-tenant secrets. Rejected: `python:3.11-slim` — ships a shell and apt, a larger CVE surface for no runtime benefit.
- **Minimal Helm chart** — one Deployment, Service, ConfigMap, Secret. Rejected: raw `kubectl apply` manifests — no templating/values, can't parameterize per-environment; Kustomize considered but Helm's packaging/versioning wins for an open-source artifact users install.
- **Pydantic Settings** for typed, env-driven, validated configuration (fail-fast on misconfiguration at boot).

### 1.2 M0 Deferred (with rationale)

| Deferred | Why it is safe to defer from M0 |
| --- | --- |
| **Rust/PyO3 hot-path kernel** | No assembly workload exists yet to benchmark; building it now is unmeasured premature optimization and a permanent build-complexity tax (PyO3 toolchain, cross-compile, distroless glibc). Gated by ADR-0001 / C14 on Month-1 evidence. |
| **SSE / streaming** | Sync-only proves the full isolation + adapter + write-back path with strictly less moving machinery. Streaming adds the write-back-tee and abort-mid-stream complexity (C8) that we want layered onto an already-correct base in Month 1. |
| **Internal gRPC** | At launch all services run **in-process** (locked architecture). gRPC is for when boundaries prove out; introducing it now is serialization overhead and an interface we'd redraw once we know the real seams. |

### 1.3 M0 Exit criteria
- `POST /v1/chat` returns a correct completion through the vLLM adapter, with a populated sync trace and a written `ContextBundle` + `ReplayResult` stub.
- The >=10,000 hostile-probe leakage gate is **green and merge-blocking**.
- All 5 data models frozen, `extra="forbid"`, ULID/RFC-3339 enforced.
- Distroless image builds via uv+Hatchling; `helm install` brings up a working pod with RLS active.

---

## 2. Milestone M1 — Alpha + Flagship (Month 1)

**Theme: the real pipeline, measured, and the differentiator visible.** Replace the M0 stubs with v1 implementations of memory, assembly, and cache; ship the Replay Debugger MVP; wire RBAC; turn on streaming. Critically, **benchmark the pure-Python Context Assembler against the < 50 ms p95 SLO** — this benchmark is the input to the Rust gate (Section 5).

### 2.1 Deliverables

#### D1.1 — Memory Engine v1
Implements the `< 100 ms p95` retrieval subsystem (hot path typically ~40 ms per the canonical facts and the Section-9 latency table).

- Pipeline: query-embed (in-process CPU **BAAI/bge-small-en-v1.5**, 384-dim, ~6 ms p95) -> **pgvector HNSW ANN** (p95 = 18 ms at launch scale <=5M vectors/tenant) **||** BM25 lexical (12 ms) -> **RRF fuse** -> rescore (6 ms). The embed is computed once and **reused** by the cache-semantic tier (no double-embed).
- Memory tiers per canonical facts: working/short-term in **Redis (TTL)**; long-term/episodic/semantic in **Postgres 16 + pgvector**.
- **C1 ownership boundary enforced:** Memory Engine returns candidates carrying **RAW per-modality scores only**. It does **not** do final ranking or budget packing. Memory-decay recency is computed against `decay_anchor` and is **orthogonal** to the assembler's lost-in-the-middle edge placement.
- **Fail-open path (canonical):** if the embedding service is unavailable, retrieval degrades to **BM25-only** with bounded recall loss **<= 12%**. This is the survivable-degraded-mode that justifies not making the embedder a Month-1 hard dependency wrapped in extra HA.
- **Candidate hard cap: <= 512** into assembly. ContextOS **never builds or owns an index** (scope-boundary invariant) — pgvector owns the HNSW index; we score/MMR over pre-retrieved candidates only.
- VectorStore adapter interface in place so the **Qdrant escape hatch** is a config swap, not a rewrite — but Qdrant itself is deferred (Section 2.2).

#### D1.2 — Context Assembler v1 (pure Python) + the gate benchmark
The **sole** final-ranking + budget-packing authority (C1). Pure Python at this milestone *by design* — its measured p95 decides the Rust gate.

- Algorithm: single weight vocabulary score -> **MMR** for diversity over the <=512 candidates -> **budget knapsack** packing under the token budget -> **edge placement** (lost-in-the-middle: highest-value items at prompt head and tail).
- **C3 tokenizer truth:** the router selects the model **before** final packing so the *correct tokenizer* enforces the hard output reserve. If the model is not yet knowable at pack time, pack against a conservative max-tokenization estimate **+ documented margin**, then **re-validate post-route** (re-pack, or fail-closed **413** if it no longer fits). The `< 50 ms p95` assembly budget **excludes** retrieval I/O and model inference.
- **Rejected alternative (assembly):** an LLM-driven "context curator" call — adds a non-deterministic, latency-unbounded, cost-bearing model call *inside* the < 50 ms budget; violates determinism (kills replay) and the scope boundary (we'd be an agent loop).
- **THE GATE BENCHMARK (input to C14, see Section 5):** run the Section-9 representative load (candidate-count distribution peaking at 512, 384-dim embeddings, target throughput) and record assembly p95. Pass/fail against < 50 ms with the headroom rule decides whether Month-3 builds the Rust kernel.

#### D1.3 — Semantic Cache v1 (two-tier)
Per-tenant-namespaced, two-tier (C5).

- **Exact tier:** Redis hash lookup, **< 1 ms p99**. The sub-1ms claim applies to **this tier only**.
- **Semantic tier:** ANN over cached query embeddings (pgvector at this scale), **8-15 ms p95** including the query embedding on miss (embedding reused from D1.1 when retrieval also runs).
- **C6 COARSE fingerprint:** `hash(normalized-query-embedding-bucket + model_id + system_prompt_version + stable_fact_set_version)`. **Memory-private-grounded responses are flagged non-cacheable** (a response grounded in one user's private memory must never be served to another). Expected **hit-ratio 25-45%** on a realistic mixed workload.
- **Rejected alternative:** a single exact-match Redis cache only — misses the paraphrase hits that drive the 15-30% caching slice of the 40-65% token-cost savings; semantic tier is where the real savings live. Rejected the other way: pure-semantic with no exact tier — pays the 8-15 ms embedding/ANN cost on the trivially-identical repeats that the < 1 ms exact tier catches free.

#### D1.4 — FLAGSHIP: Replay Debugger MVP (bit-exact rendered-prompt hash)
The wedge feature, MVP scope: **bit-exact replay of the rendered prompt**.

- Every deterministic ContextOS stage records a content-addressed hash of its decision into `ReplayResult.deterministic_stages`. The final `rendered_prompt_hash` is the sha256 of the **exact bytes** sent to the backend.
- Replay = re-execute all deterministic stages from the content-addressed, **per-tenant-encrypted** bundle and assert the recomputed `rendered_prompt_hash` equals the recorded one. **This is the MVP assertion.**
- **C7 contract (one schema):** deterministic stages = all ContextOS decisions; `backend.invoke` is **non-deterministic**; `output_equal` is asserted **only** for recorded-output replay; `live_backend=True` yields a **diff**, not byte-equality. The MVP ships the deterministic-stage + rendered-prompt assertion; what-if and context-diff replay are Month 3.
- **Rejected alternative:** logging the final prompt as plain text for "inspection" — not replay, not verifiable, not tenant-encrypted, and drifts from the live pipeline the moment code changes. Content-addressed bundles make replay an *assertion*, not a vibe.

#### D1.5 — RBAC `check()` wired in
The single authorization authority is live (C10).

- `check(principal, resource, action)` with the action enum **`{read, write, delete, admin, route, cache_read}`**.
- The router calls `check(principal, resource=model, action="route")` as the **single authority** for model-allowlist + residency; `RoutePolicy.allowed_backends` is **derived from it** — **no second policy store** (C10).
- C2 shared-org namespace becomes a real `RBACPolicy` rule (opt-in, gated); within-tenant namespace remains the hard fail-closed filter; missing/ambiguous = deny.

#### D1.6 — SSE streaming with write-back tee (C8)
Server-Sent Events streaming, layered onto the now-correct sync base.

- A **tee** splits the backend token stream: one branch streams to the client, the other accumulates for async write-back.
- **C8 terminal-event source decides:** server reaches `finish_reason` => **commit** the materialized response to write-back; client TCP-closes before the server terminal event => **discard** and attribute partial cost per C8 (prompt input cost + tokens-streamed-so-far, recorded to the budget ledger).
- **C15 streaming idempotency:** a streaming request carrying an `Idempotency-Key` that already completed returns the **materialized final response** with **zero second backend call**.

### 2.2 M1 Deferred (with rationale)

| Deferred | Why safe to defer from M1 |
| --- | --- |
| **Qdrant** | pgvector co-located in Postgres meets the launch SLO (HNSW ANN p95 = 18 ms at <=5M vectors/tenant). Qdrant's cutover (holds <=25 ms beyond that scale) is an *escape hatch behind the VectorStore adapter*, triggered by scale we don't have in alpha. Running a second datastore now is operational cost with no SLO benefit. |
| **Cross-encoder reranker** | OPT-IN, out-of-band only (scope-boundary invariant). The in-process MMR+rescore meets quality needs for alpha; a cross-encoder is a synchronous model call that would either blow the < 50 ms assembly budget or must run out-of-band — neither is alpha-critical. |
| **Abstractive compression** | Compression always runs **after** ACL/redaction (pipeline invariant). The abstractive (model-driven) compressor adds a cost-bearing inference call; alpha ships extractive/structural compression and defers the 2-4x abstractive NLI-guarded variant (>= 98% fact retention) to Month 3 where its cost enters the budget ledger properly. |
| **Cross-user cache sharing** | C6 flags memory-private-grounded responses non-cacheable. Cross-user sharing within the opt-in shared-org namespace requires the RBAC shared-namespace rule to be hardened against the leakage gate first; alpha keeps cache strictly per-(tenant, namespace) to stay trivially leak-safe. |

### 2.3 M1 Exit criteria
- Memory retrieval p95 < 100 ms; query embedding ~6 ms p95; pgvector ANN p95 = 18 ms — measured.
- **Context Assembler p95 measured against < 50 ms (the gate benchmark, Section 5) — number recorded in ADR-0001.**
- Cache hit-ratio in the 25-45% band on the mixed-workload harness; exact tier < 1 ms p99.
- Replay MVP: recorded `rendered_prompt_hash` reproduced bit-exact from the encrypted bundle.
- SSE streaming with correct C8 commit/discard semantics; RBAC `check()` is the sole route authority.
- Leakage gate still green (now exercising memory + cache surfaces).

---

## 3. Milestone M3 — Production (Month 3)

**Theme: routing, the full security/HA/observability surface, and the Rust gate resolution.** Everything required to run ContextOS as production middleware at the canonical SLOs.

### 3.1 Deliverables

#### D3.1 — Model Router v1
Difficulty + utility + circuit-breaker routing, realizing the 20-40% routing-downgrade slice of token-cost savings.

- **C9 fail posture:** hard-policy filters (allowlist, residency, capability, budget) evaluate on **STATIC policy** and **fail-CLOSED** independent of the health store. Only optimization signals (latency/queue/quality) **fail-open** to a static ranking. The **safe-default pool must itself satisfy all hard filters** — residency is **never** bypassed, even when degraded.
- **C10:** allowed backends derive from RBAC `check(action="route")`; no second policy store.
- **C3:** router selects the model **before** final packing (tokenizer truth). Router latency budget = 5 ms p95 (Section 9).
- **GPU-aware routing is a telemetry READER only** — it reads queue/utilization signals to rank; it **never schedules GPUs** (scope-boundary invariant).
- **Rejected alternative:** learned/bandit routing in v1 — needs a labeled outcome-feedback loop and online-learning safety we don't yet have; v1 is a deterministic, auditable difficulty classifier + utility function. Bandit routing is explicitly deferred (Section 3.2).

#### D3.2 — Full adapter matrix
Complete `BackendAdapter` set: vLLM (from M0), plus hosted commercial APIs and additional self-hosted runtimes, each normalizing to the OpenAI-compatible surface, each honoring the C8 abort contract and C15 idempotency identically.

#### D3.3 — Complete security
- **Prompt-injection defense** at the ACL/redaction stage (which runs **before** compression, per the pipeline invariant).
- **KMS-backed key management**; per-tenant DEKs.
- **Crypto-shred RTBF (C11):** RTBF = **tombstone + idempotent GC sweep**. Embeddings are **within crypto-shred scope** — the vector payload/id is encrypted under the per-subject DEK, so destroying the DEK renders the embedding unrecoverable. Cross-store delete is idempotent across Postgres, pgvector, Redis, and replay bundles.
- **Residency** enforcement end-to-end (router hard filter + storage placement).

#### D3.4 — Kubernetes / HA
- **7 Deployments**, each independently scalable: (1) Gateway, (2) Async/Streams consumer, (3) **Embedding service** (its own Deployment per C15, with KEDA signal, availability + cold-start NFR, and the bounded degraded BM25-only recall loss <= 12%), (4) Memory Engine workers, (5) Cache service, (6) Replay/observability service, (7) Batch/consolidation + RTBF workers.
- **KEDA** autoscaling on queue-depth / request-rate signals.
- **Multi-AZ 99.9%:** >= 3 stateless gateway replicas across >= 3 AZ, PodDisruptionBudget `minAvailable=2`.
- Gateway throughput target 5k-10k req/s per node (~0.7 vCPU per 1k req/s; hot path CPU-bound).
- **Rejected alternative:** monolith pod — couples the CPU-bound assembly hot path's scaling to the embedding service's GPU/cold-start profile; separate Deployments let KEDA scale each on its own signal.

#### D3.5 — Observability completion
- **OpenTelemetry** export. **C12 dual write paths finalized:** best-effort traces **fail-open + sampled** (tail 1-10%, force-keep on errors and on cost > $0.05/req); billing-grade cost records **fail-closed via durable outbox** (the M0 outbox, now with retry/DLQ).
- **What-if replay** (C7): re-run deterministic stages with a changed parameter, `live_backend=True` => **diff** output, not byte-equality.
- **Memory versioning** + **context diff** (compare two `ContextBundle`s' selections/ordering).
- **Agent-trace spans are READ-ONLY correlation** — observed for correlation, never scheduled or re-executed (scope-boundary invariant).

#### D3.6 — Rust kernel — ONLY IF Month-1 benchmarks failed the SLO
Per ADR-0001 / C14: build the PROVISIONAL Rust/PyO3 `ContextAssembler` **only if** the Month-1 pure-Python assembly benchmark missed the < 50 ms p95 threshold with insufficient headroom (Section 5). If Month-1 passed with headroom, **this deliverable is cancelled** and the budget is reallocated. The Rust `ContextAssembler` interface is marked **PROVISIONAL**.

### 3.2 M3 Deferred (with rationale)

| Deferred | Why safe to defer past production v1 |
| --- | --- |
| **Service mesh / mTLS** | At launch services run in-process; once split to gRPC, mesh mTLS is an infra-layer addition (Linkerd/Istio sidecar) that does not change ContextOS semantics. Network-policy + namespace isolation suffice for v1; mesh is operational hardening, not correctness. |
| **Kafka** | Redis Streams + the custom asyncio consumer (which doubles as the replay log) meets v1 throughput and is one fewer system to operate. Kafka's partition/retention scale matters only beyond v1 ingest volumes; the consumer interface abstracts the swap. |
| **Multi-region residency** | v1 enforces single-region residency as a hard, fail-closed router filter (C9). Multi-region (active-active, regional bundle routing) is a topology expansion, not a v1 correctness requirement — single-region residency is *correct*, just not geo-distributed. |
| **Learned / bandit routing** | Requires the outcome-feedback + difficulty-classifier labeling pipeline (M3 deliverable) to accumulate labeled data first, plus online-learning safety rails. Deterministic difficulty + utility routing is auditable and replay-friendly; bandits come after we have the data and the eval harness to trust them. |

### 3.3 M3 Exit criteria
- Total ContextOS control overhead < 250 ms p95 (critical-path, not naive sum; Section 9).
- 99.9% gateway/control-plane availability under PDB + multi-AZ chaos test.
- Crypto-shred RTBF verified: post-shred, the subject's embeddings/records/replay bundles are cryptographically unrecoverable; leakage gate still green.
- Token-cost savings demonstrated in the 40-65% band (caching 15-30% + routing 20-40% of model spend + compression 10-25%).
- ADR-0001 closed: Rust kernel built-and-passing, or formally cancelled with the benchmark evidence attached.

---

## 4. Component-to-Milestone Map (every component, exhaustively)

Each component named anywhere in the design, mapped to the milestone where it **first ships in production form**. "Stub" = call-signature-compatible no-op present earlier so the swap is non-breaking.

| Component | M0 (Wk 1-2) | M1 (Month 1) | M3 (Month 3) |
| --- | --- | --- | --- |
| Gateway `POST /v1/chat` (sync) | **Ship** | (SSE added) | — |
| BackendAdapter — vLLM | **Ship** | — | — |
| Full adapter matrix (hosted + self-hosted) | — | — | **Ship** |
| SecurityContext + tenant resolve | **Ship** | — | — |
| Postgres FORCE RLS + `SET LOCAL app.tenant_id` | **Ship** | — | — |
| `tenant_id` partition key | **Ship** | — | — |
| App RBAC firewall (repo boundary) | **Ship** | (RBAC `check()`) | — |
| >=10k hostile-probe leakage CI gate | **Ship** | (memory+cache surfaces) | (RTBF surfaces) |
| 5 canonical data models | **Ship (frozen)** | — | — |
| Sync trace stub / cost outbox | **Ship** | — | (OTel + sampling, C12) |
| Pydantic Settings / uv+Hatchling / distroless / Helm | **Ship** | — | (7-Deployment chart) |
| Memory Engine v1 (embed + ANN \|\| BM25 + RRF + rescore) | stub | **Ship** | — |
| Context Assembler v1 (pure Python, score+MMR+knapsack+edge) | passthrough stub | **Ship + benchmark** | — |
| Semantic Cache v1 (exact Redis + semantic ANN) | always-miss stub | **Ship** | (cross-org sharing rule) |
| Replay Debugger — bit-exact rendered-prompt hash | ReplayResult stub | **Ship (MVP)** | (what-if + context diff) |
| RBAC `check()` (single authority, action enum incl. route) | firewall only | **Ship** | — |
| SSE streaming + write-back tee (C8) | — | **Ship** | — |
| Idempotency-Key materialized-response (C15) | — | **Ship** | — |
| VectorStore adapter (pgvector default) | — | **Ship** | — |
| Qdrant escape hatch | — | deferred | (config-swap on scale trigger) |
| **Compressor** (extractive/structural) | — | **Ship (extractive)** | — |
| **Compressor — abstractive NLI-guarded (2-4x, >=98% retention)** | — | deferred | **Ship** |
| Cross-encoder reranker (opt-in, out-of-band) | — | deferred | **Ship (opt-in)** |
| Model Router v1 (difficulty+utility+breaker, C9/C10) | static single backend | — | **Ship** |
| **Embedding service** (own K8s Deployment, KEDA, cold-start NFR, C15) | in-process lib | in-process (~6 ms p95) | **Ship as Deployment** |
| Prompt-injection defense (pre-compression) | — | — | **Ship** |
| KMS / per-tenant DEK | — | — | **Ship** |
| **Crypto-shred RTBF workers** (tombstone + idempotent GC, C11) | — | — | **Ship** |
| Residency enforcement (router + storage) | — | (router stub) | **Ship** |
| K8s / KEDA / multi-AZ HA (7 Deployments, 99.9%) | minimal Helm | — | **Ship** |
| OpenTelemetry export | sync stub | — | **Ship** |
| **Cost outbox** (billing-grade, fail-closed durable, C12) | **Ship (Postgres outbox)** | — | (retry/DLQ + OTel) |
| Best-effort sampled trace path (tail 1-10%, force-keep, C12) | — | — | **Ship** |
| What-if replay / context diff / memory versioning (C7) | — | — | **Ship** |
| Memory consolidation batch job (async, rate-limited, cost-tracked) | — | — | **Ship** |
| **Cache-eval / cache-correctness harness** (C6 roadmap item) | — | (hit-ratio harness) | **Ship (correctness eval)** |
| **Threshold-calibration job** (semantic-cache + difficulty thresholds) | — | — | **Ship** |
| **Difficulty-classifier labeling pipeline** | — | — | **Ship** |
| Async plane (Redis Streams + asyncio consumer = replay log) | — | **Ship** | (DLQ/retry) |
| **Rust/PyO3 ContextAssembler kernel (PROVISIONAL)** | deferred | (gate benchmark) | **conditional — Section 5** |
| Internal gRPC boundaries | deferred (in-process) | deferred | (when boundaries prove out) |
| Service mesh / mTLS | deferred | deferred | deferred (post-v1) |
| Kafka | deferred | deferred | deferred (post-v1) |
| Multi-region residency | deferred | deferred | deferred (post-v1) |
| Learned/bandit routing | deferred | deferred | deferred (post-v1) |

---

## 5. The Rust Kernel Gate (ADR-0001 / C14)

The Rust/PyO3 hot-path `ContextAssembler` is **PROVISIONAL and conditional**. It is built only if a *measured* failure of the pure-Python assembler demands it. This section is the binding decision procedure.

### 5.1 Why gated, not default
A Rust kernel is a permanent tax: PyO3 build toolchain, cross-compilation, ABI/glibc pinning in the distroless image, a second language in an otherwise-Python contributor base, and a FFI boundary to debug. We pay that tax **only** against measured evidence that pure Python cannot hold the < 50 ms p95 assembly budget. **Rejected alternative:** "Rust by default because hot path" — the hot path is `score(<=512) + MMR + knapsack + edge-place` over 384-dim vectors; that may well fit in Python+NumPy within budget, and shipping Rust unmeasured is exactly the premature optimization we forbid.

### 5.2 Benchmark methodology (run in M1)
- **Workload:** the Section-9 representative request mix. **Candidate-count distribution** spanning the realistic range with mass concentrated at the **hard cap of 512** (worst case dominates a p95 gate). **384-dim embeddings** (BAAI/bge-small-en-v1.5). Per-candidate `raw_scores` populated as in production.
- **Operations measured (assembly only):** weight-vocabulary scoring over <=512 candidates, MMR diversity selection, budget knapsack packing, edge placement. **Excludes** retrieval I/O and model inference (per the canonical assembly budget definition).
- **Target throughput:** the per-node assembly rate implied by the gateway's 5k-10k req/s per node at the assembly stage's share of CPU (~0.7 vCPU per 1k req/s, hot path CPU-bound) — i.e., the benchmark drives the assembler at the concurrency a single node sustains, not single-request micro-timing.
- **Environment:** the production distroless image on the target node CPU class (no dev-laptop numbers), warm process (post-JIT/allocator warmup), measured over a statistically stable window.
- **Metric:** assembly-stage **p95 latency** under sustained target throughput.

### 5.3 Threshold + headroom (the trigger)
- **Hard SLO:** assembly p95 **< 50 ms** (canonical).
- **Headroom rule:** the gate requires the measured pure-Python p95 to sit at **<= 35 ms (i.e., >= 30% headroom under the 50 ms budget)** to be declared a *pass*. The 30% headroom absorbs: production tail-load variance, future candidate-feature growth in the score vocabulary, and CPU contention from co-tenant load on the node.
- **Decision:**
  - Measured p95 **<= 35 ms** => **PASS**. Pure Python stays. The Rust kernel deliverable (D3.6) is **cancelled** and its Month-3 capacity is reallocated. ADR-0001 closed as "Python sufficient" with the benchmark attached.
  - Measured p95 **> 35 ms and < 50 ms** => **CONDITIONAL**. In-budget but under-headroom: first attempt bounded Python optimization (NumPy vectorization of scoring/MMR, pre-sized buffers, removing per-candidate Python-object overhead). Re-benchmark. If still > 35 ms, **trigger Rust** for the scoring/MMR inner loops only.
  - Measured p95 **>= 50 ms** => **FAIL** => **trigger Rust** (D3.6 built in Month 3). The PROVISIONAL Rust `ContextAssembler` interface is implemented behind the unchanged Python `ContextAssembler` Protocol so callers are untouched.

### 5.4 Scope of a triggered Rust kernel
Even when triggered, Rust is confined to the **CPU-bound inner loops** (scoring over <=512 candidates, MMR pairwise-similarity selection, knapsack DP) exposed through PyO3. Orchestration, policy, tokenizer binding (C3), and edge placement stay in Python. We do **not** rewrite the assembler in Rust; we replace its hottest numeric core. The Rust interface stays marked **PROVISIONAL** until it has run a full production cycle.

### 5.5 Owner & accountability
- **Owner:** the **Context Assembly subsystem lead** (the engineer accountable for the < 50 ms SLO) owns the benchmark, runs it in M1, records the result and decision in **ADR-0001**, and — if triggered — owns the Rust kernel through to its first production cycle and the removal of the PROVISIONAL marker.
- **Gate artifact:** ADR-0001 must contain the raw benchmark output, the measured p95, the headroom calculation, and the explicit PASS/CONDITIONAL/FAIL decision. The milestone M3 cannot close with ADR-0001 open.

---

## 6. Roadmap invariants (do not drift)

- The leakage gate (>=10k hostile probes, leakage = 0) is green at **every** milestone; new surfaces (memory, cache, RTBF) extend it, never relax it.
- The pipeline ordering invariant — `auth/tenant -> cache -> retrieve -> ACL/redaction -> compression -> assembly -> routing -> adapter -> stream -> write-back` — holds from M0 (as stubs) onward; **compression always after ACL/redaction**.
- ContextOS never becomes an LLM, inference engine, vector DB, training system, or agent framework. Every milestone's components respect the scope-boundary invariants (in-process scoring over <=512 pre-retrieved candidates; read-only agent-trace correlation; cost-tracked async consolidation; telemetry-reader GPU routing).
- All latency claims defer to the Section-9 authoritative table; this section restates none of them in conflict.
