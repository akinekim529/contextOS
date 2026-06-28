# 05 — Deployment Design

This section specifies how ContextOS is packaged, scheduled, scaled, and made
durable on Kubernetes. It is opinionated: every chart-layout, scaling-signal,
backing-store, and topology choice below names the alternative we rejected and
why it fails for replay-grade, multi-tenant LLM middleware. ContextOS is
**middleware** — it owns memory, context assembly under a token budget,
multi-tenant isolation, semantic caching, model routing, and replay-grade
observability. It is **not** an LLM, an inference engine, a vector DB, a
training system, or an agent framework, and the deployment topology is
deliberately shaped to keep it from drifting into any of those (C13). The
embedding service is the only component that touches a model artifact, and even
it is a CPU-bound encoder, not an inference plane.

The latency numbers cited here (assembly < 50 ms p95, retrieval < 100 ms p95,
total control overhead < 250 ms p95, pgvector HNSW ANN probe 18 ms p95, exact
cache < 1 ms p99, semantic cache 8–15 ms p95, query embedding ~6 ms p95) are
**owned by Section 9's authoritative latency budget**. This section references
them to justify resource requests, replica counts, and scaling thresholds; it
never restates a different figure.

---

## 1. Packaging Decisions

### 1.1 uv + Hatchling, distroless, Helm

| Concern | Choice | Rejected alternative | Why the rejected option fails |
|---|---|---|---|
| Dependency resolution / lock | **uv** (single resolver, `uv.lock`) | Poetry | Poetry's resolver is 10–50x slower on our dep graph and its lockfile is not reproducible across platforms without `--no-binary` gymnastics; uv produces a single universal lock and rebuilds CI images in seconds. |
| Build backend | **Hatchling** | setuptools / Flit | setuptools `setup.py` is imperative and hard to audit for supply-chain; Flit can't express the PyO3/maturin conditional build needed if the Rust kernel ships (C14). Hatchling's plugin model lets one `pyproject.toml` drive both pure-Python wheels and an optional native extension. |
| Container base | **distroless** (`gcr.io/distroless/python3-debian12`) | `python:3.11-slim`, Alpine | slim ships apt, a shell, and ~80 CVE-bearing packages we never call; Alpine's musl breaks `pgvector`/`numpy`/`onnxruntime` wheels and forces source builds. Distroless gives no shell, no package manager, a ~40 MB attack surface, and SBOM-clean images — mandatory for a system whose threat model is cross-tenant leakage. |
| Orchestration packaging | **Helm 3 umbrella chart** | Kustomize, raw manifests, Operators-for-everything | Kustomize can't express the conditional "assembler in-gateway vs. own Deployment" toggle (Section 3) without overlay sprawl; raw manifests duplicate the PDB/HPA/NetworkPolicy boilerplate across 7 services. Helm's library-chart pattern lets us define those three policies **once** and have every subchart inherit them. |

Distroless has no shell, so **liveness/readiness probes are HTTP**
(`GET /healthz`, `GET /readyz`) served by each FastAPI/Starlette app — never
`exec`-based. Worker and embedding Deployments expose a tiny aiohttp health
server on a sidecar port for the same reason.

---

## 2. Helm Chart Layout

ContextOS ships as **one umbrella chart** that depends on **one library chart**
(`contextos-common`) and **seven application subcharts**. The library chart
emits no Kubernetes objects of its own — it is a pure template library (Helm
`type: library`) that the subcharts `include` to render their PodDisruption
Budget, HorizontalPodAutoscaler/KEDA `ScaledObject`, NetworkPolicy, ServiceAccount,
and topology-spread blocks from a single source of truth.

```
deploy/charts/
├── contextos/                      # umbrella (type: application)
│   ├── Chart.yaml                  # dependencies: contextos-common + 7 subcharts
│   ├── values.yaml                 # global + per-service overrides
│   ├── values-prod.yaml            # 3-AZ, RLS-strict, KEDA on
│   ├── values-dev.yaml             # single-AZ, assembler in-gateway, KEDA off
│   └── templates/
│       ├── NOTES.txt
│       ├── _global-checks.tpl      # fail-render if tenant isolation knobs unset
│       └── namespace.yaml
│
├── contextos-common/               # LIBRARY chart (type: library) — renders nothing alone
│   ├── Chart.yaml                  # type: library
│   └── templates/
│       ├── _pdb.tpl                # define "contextos-common.pdb"
│       ├── _hpa.tpl                # define "contextos-common.keda-scaledobject"
│       ├── _networkpolicy.tpl      # define "contextos-common.netpol" (default-deny + allowlist)
│       ├── _topologyspread.tpl     # define "contextos-common.topologySpread"
│       ├── _podsecurity.tpl        # define "contextos-common.securityContext" (non-root, RO-rootfs)
│       ├── _serviceaccount.tpl     # define "contextos-common.serviceaccount"
│       ├── _resources.tpl          # define "contextos-common.resources" (named tier presets)
│       └── _labels.tpl             # define "contextos-common.labels" (tenant-aware, app.k8s.io/*)
│
└── charts/                         # the 7 application subcharts
    ├── gateway/                    # Deployment 1 — stateless, OpenAI-compatible /v1
    ├── memory/                     # Deployment 2 — retrieval (candidates-only)
    ├── assembler/                  # Deployment 3 — packed-disabled at launch (in-gateway)
    ├── cache/                      # Deployment 4 — two-tier cache control plane
    ├── router/                     # Deployment 5 — model routing + breaker
    ├── worker/                     # Deployment 6 — Redis Streams consumers / write-back
    └── embedding/                  # Deployment 7 — BGE encoder (C15, own Deployment)
```

Backing stores (Postgres, Redis, optional Qdrant) are **not** subcharts. They
are provisioned by operators (CloudNativePG, Redis Operator) or external managed
services and referenced by connection secrets. We rejected bundling Bitnami
`postgresql`/`redis` subcharts inside the umbrella because a single `helm upgrade`
must never be able to restart or re-provision a stateful, PITR-protected,
per-tenant-encrypted datastore as a side effect of a stateless gateway rollout.
Lifecycle of stateful data is decoupled from lifecycle of stateless compute by
construction.

### 2.1 Library-chart usage (why a library chart, not copy-paste)

Every subchart's `templates/scaledobject.yaml`, `pdb.yaml`, and
`networkpolicy.yaml` are three-line shims:

```yaml
# charts/gateway/templates/pdb.yaml
{{- include "contextos-common.pdb" (dict "ctx" . "minAvailable" 2) }}
```

```yaml
# charts/gateway/templates/networkpolicy.yaml
{{- include "contextos-common.netpol" (dict "ctx" . "ingressFrom" (list "ingress-nginx")
      "egressTo" (list "router" "memory" "cache" "embedding" "postgres" "redis")) }}
```

Rejected alternative: per-service hand-written PDB/HPA/NetworkPolicy. With seven
services that produces ~21 near-identical files where one drifts (a `minAvailable: 1`
slips into the gateway PDB) and silently breaks the 99.9% guarantee. The library
chart makes "every front-of-house Deployment has `minAvailable=2` and a
default-deny NetworkPolicy" an **invariant enforced at render time**, not a review
hope. `helm template | conftest` then gates it in CI.

---

## 3. Module → Deployment Mapping (7 Deployments)

ContextOS's pipeline modules map onto exactly seven Deployments. The
**pipeline ordering invariant** is preserved regardless of deployment boundary:
`auth/tenant → cache lookup → retrieve candidates → ACL/redaction → compression →
assembly/packing → routing → adapter → stream → async write-back`. Compression
**always** runs after ACL/redaction; splitting modules across Deployments does
not reorder them.

| # | Deployment | Modules it hosts | Stateful? | Hot-path? | Launch posture |
|---|---|---|---|---|---|
| 1 | **gateway** | FastAPI `/v1`, auth/tenant resolve + RLS `SET LOCAL`, ACL/redaction, compression, **assembler (embedded at launch)**, adapter dispatch, SSE streaming | No | Yes (CPU-bound) | Owns the critical path |
| 2 | **memory** | query-embed call-out, pgvector ANN ‖ BM25, RRF fuse, raw per-modality rescore — returns **candidates only** (C1) | No (data in PG/Redis) | Yes | Own service from day one |
| 3 | **assembler** | score+MMR+budget-knapsack over ≤512 candidates, edge-placement | No | Yes | **In-gateway at launch**, own Deployment later |
| 4 | **cache** | exact-hash (Redis) + semantic-ANN (pgvector/Qdrant) coordination, fingerprinting (C6) | No (data in Redis/PG) | Yes | Own service from day one |
| 5 | **router** | difficulty/utility scoring, RBAC `route` check (C10), breaker, GPU telemetry reader (C9) | No | Yes | Own service from day one |
| 6 | **worker** | Redis Streams consumers: write-back, memory consolidation (rate-limited, cost-tracked batch), GC sweeps (C11), replay-bundle sealing | No (state in streams/PG) | No (off hot path) | Own service from day one |
| 7 | **embedding** | self-hosted BAAI/bge-small-en-v1.5 (384-dim) encoder, live + batch lanes (C15) | No (model in image/PVC-RO) | Yes (live lane) | **Own Deployment from day one** |

### 3.1 Assembler: in-gateway at launch, own service later

At launch the **Context Assembler runs in-process inside the gateway**. The
assembler subchart exists but renders zero replicas (`assembler.enabled: false`),
and the gateway imports the assembler module directly. Rationale: assembly is a
pure CPU computation (score + MMR + knapsack over ≤512 candidates, target
**< 50 ms p95**, Section 9) over candidates the gateway already holds. A network
hop to a separate assembler service would add a serialization round-trip for a
≤512-item candidate list on the **critical path** — pure latency tax with zero
isolation benefit while assembly is stateless and co-tenant-safe.

We make it a **promotable boundary** rather than a permanent merge because two
futures pull it out:

1. **The Rust/PyO3 kernel (C14, ADR-0001).** If the in-process Python assembler's
   p95 crosses its threshold under the benchmark (candidate-count distribution up
   to 512, 384-dim embeddings, target req/s defined in Section 9), the Rust
   `ContextAssembler` (interface **PROVISIONAL**) ships as a PyO3 extension —
   still in-gateway first, but the clean module boundary makes the swap a
   dependency change, not a rewrite.
2. **Independent scaling.** If assembly CPU dominates gateway CPU at high
   candidate counts, promoting it to Deployment #3 lets it scale on its own KEDA
   signal without over-provisioning the I/O-bound gateway.

The toggle:

```yaml
# values.yaml (umbrella)
assembler:
  enabled: false            # launch: in-gateway. Flip true to promote to own Deployment.
  embeddedInGateway: true   # gateway imports contextos.assembler when assembler.enabled=false
```

Rejected alternative: ship assembler as its own Deployment from day one "for
microservice purity." That pays a guaranteed per-request network hop now to buy
flexibility we can add later at zero cost via the toggle. Latency on the killer
path is not spent speculatively.

---

## 4. Per-Service Scaling (KEDA)

We use **KEDA `ScaledObject`s**, not bare HPA-on-CPU, for every service whose
true load signal is a **queue or request rate** rather than CPU. CPU-only HPA
lags badly for bursty LLM traffic: by the time CPU climbs, the Redis Streams
backlog or the request queue is already deep and p95 has blown the SLO. KEDA
scales on the *cause* (QPS, stream depth), CPU HPA scales on the *symptom*. KEDA
also gives us **scale-to-floor (not zero)** for stateful-ish services with model
cold-start.

| Deployment | KEDA trigger | Metric source | Why this signal |
|---|---|---|---|
| **gateway** | **QPS** | Prometheus `sum(rate(http_requests_total[1m]))` per replica | Gateway is hot-path CPU-bound at ~0.7 vCPU per 1k req/s; one node sustains 5k–10k req/s. QPS is the direct driver of proxy+assembly CPU. |
| **worker** | **Stream / queue depth** | Redis Streams `XINFO GROUPS` lag (pending entries) | Write-back, consolidation, and GC are throughput jobs; depth-of-backlog is the only honest signal. CPU on a consumer idles between batches. |
| **embedding** | **Queue depth (batch lane) + QPS (live lane)** | Redis batch-embed stream lag **and** live-embed Prometheus QPS | Two lanes, two signals (C15): batch consolidation embeds tolerate latency and scale on backlog; live query embeds are on the retrieval hot path (~6 ms p95) and scale on QPS to protect the < 100 ms retrieval SLO. |
| **router** | **QPS** | Prometheus router-decision rate | Routing is a ~5 ms p95 CPU decision per request; scales 1:1 with gateway QPS. |
| **memory** | **QPS** | Prometheus retrieval-request rate | Retrieval is hot-path; scale with incoming query rate, not CPU symptom. |
| **cache** | **QPS** | Prometheus cache-lookup rate | Exact tier is < 1 ms p99; semantic tier 8–15 ms p95. QPS drives the semantic-embed work. |
| **assembler** *(when promoted)* | **QPS** | Prometheus assembly-request rate | Pure CPU over ≤512 candidates; QPS is the driver. Disabled while in-gateway. |

KEDA `ScaledObject` for the gateway (rendered from `contextos-common.keda-scaledobject`):

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: { name: gateway }
spec:
  scaleTargetRef: { name: gateway }
  minReplicaCount: 3          # 99.9% floor: >=3 across >=3 AZ, never below
  maxReplicaCount: 40
  cooldownPeriod: 120
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleDown: { stabilizationWindowSeconds: 300 }   # damp flap on bursty LLM traffic
        scaleUp:   { stabilizationWindowSeconds: 0, policies: [{ type: Percent, value: 100, periodSeconds: 30 }] }
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prometheus.monitoring:9090
        query: sum(rate(contextos_gateway_requests_total[1m]))
        threshold: "800"      # ~0.8k req/s/replica -> ~0.56 vCPU headroom under the 0.7 vCPU/1k budget
```

Worker `ScaledObject` (stream depth):

```yaml
  triggers:
    - type: redis-streams
      metadata:
        address: redis-master.contextos:6379
        stream: contextos:writeback
        consumerGroup: writeback-workers
        pendingEntriesCount: "500"   # scale up when >500 unacked write-backs pile up
```

Embedding `ScaledObject` (dual-lane, C15):

```yaml
  minReplicaCount: 2                 # never scale to zero: avoids model cold-start on the hot path
  triggers:
    - type: prometheus               # live lane
      metadata: { query: sum(rate(contextos_embed_live_requests_total[1m])), threshold: "600" }
    - type: redis-streams            # batch lane
      metadata: { stream: contextos:embed-batch, consumerGroup: embed-batch, pendingEntriesCount: "2000" }
```

Rejected alternative: HPA on CPU/memory for all services. It under-scales bursty
gateways (CPU is a lagging indicator of request-queue depth) and **cannot scale
the worker at all** — a consumer with a 50k-entry backlog can show 20% CPU
between batches and HPA would keep it at one replica while the replay log and
write-back queue grow unbounded.

### 4.1 The embedding service must not scale to zero

`minReplicaCount: 2` on embedding is deliberate (C15). BGE-small loads its
ONNX weights (~130 MB) into the process; a cold start is seconds, and a cold
start on the **live** lane directly inflates the ~6 ms p95 query-embed step and
cascades into the < 100 ms retrieval SLO. The **availability/cold-start NFR**:
embedding live-lane p99 cold-start contribution ≤ 0 in steady state (warm floor
of 2 replicas across 2 AZ), and a fresh replica reaches readiness (model loaded,
one warm-up encode) within **15 s** before KEDA routes traffic to it
(`readinessProbe` gates on a sentinel encode). If the embedding service is
**fully unavailable**, retrieval degrades to **BM25-only** with **bounded recall
loss ≤ 12%** — the fail-open path is wired at the memory service, not a hard
outage.

---

## 5. GPU-Aware Routing: Telemetry Reader Only

The router integrates with **vLLM** backends as a **telemetry READER**. It scrapes
vLLM's `/metrics` (`vllm:num_requests_running`, `vllm:num_requests_waiting`,
`vllm:gpu_cache_usage_perc`) and folds queue depth / KV-cache pressure into the
**optimization signal** of a routing decision. It **never schedules a GPU, never
sizes a GPU pool, never preempts a vLLM request** (scope-boundary invariant, C13).

```
[ vLLM Deployment(s) ]  --(/metrics scrape, READ-ONLY)-->  [ Prometheus ]
                                                                 |
                                                          (PromQL query)
                                                                 v
                                                          [ router ]  --route decision-->
```

Router fail posture (C9) is reflected in deployment wiring:

- **Hard-policy filters** (allowlist, residency, capability, budget) evaluate on
  **static policy** loaded from a ConfigMap/Secret and **fail closed**,
  independent of whether Prometheus or the vLLM `/metrics` endpoint is reachable.
  Residency is never bypassed; the safe-default backend pool itself satisfies
  every hard filter.
- **Optimization signals** (vLLM queue depth, observed latency, quality) **fail
  open to a static ranking** if the telemetry scrape is stale or down.

The router calls `check(principal, resource=model, action='route')` as the single
authority for model-allowlist + residency (C10); `RoutePolicy.allowed_backends`
derives from it, with no second policy store in the deployment. The vLLM
Deployments live in their own namespace/node-pool (GPU nodes) that ContextOS
**reads from but does not own** — confirming ContextOS is not an inference plane.

Rejected alternative: a router that calls a Kubernetes `Scale` API or a GPU
autoscaler when vLLM queues grow. That would make ContextOS a scheduler/inference
controller and violate the scope boundary; GPU capacity is the platform team's
concern, surfaced to us only as telemetry.

---

## 6. Backing-Store Choices

| Store | Role | Operator / form | Rejected alternative | Why rejected |
|---|---|---|---|---|
| **Redis** | Exact-hash cache tier (< 1 ms p99), Redis Streams async plane (= replay log), working/short-term memory (TTL), KEDA queue signal | Redis Operator (or managed), **AOF on**, per-tenant key namespacing | Kafka for the async plane | Kafka is a heavyweight distributed log we don't need at launch scale; Redis Streams gives us at-least-once consumer groups, sub-ms enqueue (2 ms write-back enqueue, Section 9), and doubles as the **replay log** in one system. We keep Kafka as a future cutover, not a launch dependency. |
| **Postgres 16 + pgvector** | Relational truth (tenant_id partition key, FORCE RLS), long-term/episodic/semantic memory, **co-located HNSW vectors** (18 ms p95 ANN probe), semantic-cache ANN tier | **CloudNativePG operator**, PITR | Standalone vector DB (Pinecone/Weaviate) from day one | Co-locating vectors **inside** Postgres means one tenant-isolation mechanism (RLS), one transaction for "row + its embedding," one backup/PITR story, and one place to crypto-shred on RTBF (C11). A separate vector DB doubles the isolation surface and creates write-skew between row and vector. pgvector HNSW holds 18 ms p95 to ≤5M vectors/tenant. |
| **Qdrant** | **Escape hatch** only — engaged when a tenant exceeds ~5M vectors and pgvector ANN risks crossing 18 ms p95 | Optional StatefulSet / managed, behind the `VectorStore` adapter | Default to Qdrant for everyone | Defaulting to Qdrant pays the dual-store isolation cost for every tenant to solve a problem only the largest few have. Behind the adapter, cutover is per-tenant config; Qdrant holds the vector query ≤ 25 ms p95 beyond launch scale. |

Postgres is the **only true stateful store** in the ContextOS data plane that we
operate; Redis is durable-but-cache-shaped (AOF). The `VectorStore`,
`EmbeddingProvider`, and cache adapters mean these are **swap-by-config**
boundaries, not load-bearing assumptions baked into business logic.

### 6.1 CloudNativePG: why an operator, not a StatefulSet

We rejected a hand-rolled Postgres `StatefulSet`. Operating RLS-strict,
PITR-protected, per-tenant-encrypted Postgres by hand means owning failover,
backup, WAL archiving, and minor-version upgrades in shell scripts. CloudNativePG
gives **declarative PITR** (continuous WAL archiving to object storage),
**automated failover** with synchronous replicas across AZs, and
**rolling minor upgrades** — all as CRDs the umbrella references but does not
manage. Crucially, `FORCE ROW LEVEL SECURITY` and the `tenant_id` partition key
are schema concerns enforced regardless of operator; the operator handles
durability, not isolation.

```yaml
# referenced by the umbrella, owned by the platform — NOT a subchart
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: { name: contextos-pg }
spec:
  instances: 3                                   # 1 primary + 2 sync replicas across 3 AZ
  postgresql:
    parameters: { shared_preload_libraries: "vector" }   # pgvector
  bootstrap: { initdb: { postInitSQL: ["ALTER DATABASE app FORCE ROW LEVEL SECURITY"] } }
  backup:
    retentionPolicy: "30d"
    barmanObjectStore: { destinationPath: "s3://contextos-pitr/", wal: { compression: gzip } }
```

---

## 7. Multi-Node / Multi-AZ Topology for 99.9%

The **availability target is 99.9%** for the gateway/control plane. The
deployment mechanics that buy it:

- **≥ 3 stateless replicas of every front-of-house Deployment, spread across ≥ 3
  Availability Zones.** A single-AZ outage leaves ≥ 2 replicas serving.
- **PodDisruptionBudget `minAvailable: 2`** on every front-of-house Deployment —
  rendered once from `contextos-common.pdb`. Voluntary disruptions (node drains,
  rolling upgrades) can never take more than one replica below the floor.
- **`topologySpreadConstraints` with `maxSkew: 1` over `topology.kubernetes.io/zone`**
  and `whenUnsatisfiable: DoNotSchedule` — the scheduler must spread, not pile
  three replicas into one zone.
- **Pod anti-affinity** (soft, preferred) over `kubernetes.io/hostname` so two
  replicas avoid the same node where the cluster allows.

```yaml
# rendered by contextos-common.topologySpread for every front-of-house Deployment
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector: { matchLabels: { app.kubernetes.io/name: gateway } }
---
# contextos-common.pdb
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: gateway }
spec:
  minAvailable: 2
  selector: { matchLabels: { app.kubernetes.io/name: gateway } }
```

| Deployment | Min replicas | AZ spread | PDB minAvailable |
|---|---|---|---|
| gateway | 3 | ≥ 3 | 2 |
| memory | 3 | ≥ 3 | 2 |
| cache | 3 | ≥ 3 | 2 |
| router | 3 | ≥ 3 | 2 |
| embedding | 2 | ≥ 2 | 1 (warm floor; KEDA holds ≥ 2) |
| worker | 2 | ≥ 2 | 1 (off hot path; backlog-tolerant) |
| assembler *(promoted)* | 3 | ≥ 3 | 2 |
| Postgres (CloudNativePG) | 3 (1 primary + 2 sync) | ≥ 3 | operator-managed |
| Redis | 3 (1 primary + 2 replica) | ≥ 3 | operator-managed |

**Throughput math.** One gateway node sustains 5k–10k req/s
(~0.7 vCPU per 1k req/s of proxy+assembly; hot path is CPU-bound). Three replicas
across three AZ give 15k–30k req/s headroom while tolerating a full-AZ loss
(2 replicas, 10k–20k req/s). KEDA on QPS (Section 4) scales beyond this; the
3-replica floor is the **availability** minimum, not the **capacity** ceiling.

Rejected alternative: 2 replicas in 2 AZ "to save cost." With only 2 replicas a
PDB `minAvailable: 2` blocks every voluntary node drain (you can never take one
down), and a single-AZ failure during a rolling upgrade drops you to zero
healthy. Three-across-three is the minimum that survives one AZ loss *and* allows
rolling maintenance. We do not negotiate the floor below it.

---

## 8. Statefulness & Durability

ContextOS is built so that **all hot-path compute is stateless and all state
lives in operator-managed or external stores**. This is what makes the gateway,
router, and assembler trivially horizontally scalable and zone-fault-tolerant.

| Component | Stateful? | Durability mechanism | Restart behavior |
|---|---|---|---|
| gateway | **Stateless** | none (request-scoped only) | Replaceable; drains in-flight SSE on `SIGTERM` (graceful, `terminationGracePeriodSeconds: 30`) |
| assembler | **Stateless** | none | Replaceable instantly |
| router | **Stateless** | static policy from ConfigMap/Secret; health-store is cache-only | Fails closed on hard filters without health store (C9) |
| memory | **Stateless** | data in Postgres/Redis | Replaceable |
| cache | **Stateless** | data in Redis/Postgres | Replaceable; cache miss on cold start, no correctness loss |
| embedding | **Stateless** *(model in RO image/PVC)* | model artifact baked into image (immutable) or mounted RO | Warm floor of 2 (C15); 15 s readiness on cold replica |
| worker | **Stateless** *(progress in stream)* | consumer-group offsets in Redis Streams; at-least-once + idempotent handlers | Resumes from last ack; duplicates safe |
| **Postgres 16 + pgvector** | **STATEFUL** | CloudNativePG: sync replicas across AZ + **PITR** (continuous WAL to object store, 30-day retention) | Operator failover; primary promotion |
| **Redis** | **Durable-cache** | **AOF** (`appendfsync everysec`) + cross-AZ replica | AOF replay on restart; cache tier rebuilds on loss without correctness impact |

### 8.1 Why Redis AOF, not RDB-only

Redis here is not a pure cache — it is also the **async plane / replay log** and
the working/short-term memory tier. RDB snapshots alone would lose up to the
snapshot interval of Streams entries on crash, which means **lost write-back and
lost replay-log entries** — unacceptable for a replay-grade system. **AOF with
`appendfsync everysec`** bounds worst-case loss to ~1 s of stream appends while
keeping enqueue latency at the 2 ms p95 the Section 9 budget allows. The
**exact-hash cache tier** can tolerate loss (it rebuilds on miss), but it shares
the instance with the Streams log, so AOF protects both. RDB stays on as a
fast-restart base layer; AOF is the durability guarantee.

### 8.2 Durability of the replay bundle (write-path note)

The Context Replay Debugger's content-addressed, per-tenant-encrypted bundle is
**sealed by the worker Deployment** (off the hot path) and persisted to
object storage referenced from Postgres (bundle digest + tenant DEK reference in
an RLS-protected row). Trace write-paths follow C12 at the deployment level:
**best-effort traces** are fail-open and tail-sampled (1–10%, force-keep errors
and any request with cost > $0.05); **billing-grade cost records** go through a
**fail-closed durable outbox** (Postgres outbox table drained by the worker),
because a dropped cost record corrupts the budget ledger that memory
consolidation and routing-budget filters depend on. The two write paths are
physically separated: a trace-collector sink (lossy, sampled) and a Postgres
outbox (durable, exactly-once-on-drain).

---

## 9. values.yaml Surface Sketch

The umbrella `values.yaml` exposes a `global` block (tenant-isolation,
backing-store endpoints, topology defaults) plus a per-service block. The
`contextos-common` library reads `global.topology` and per-service overrides to
render PDB/HPA/NetworkPolicy. A render-time check in `_global-checks.tpl` **fails
the install** if tenant-isolation knobs are left at unsafe defaults — you cannot
`helm install` ContextOS with RLS disabled.

```yaml
global:
  image:
    registry: ghcr.io/contextos
    tag: "1.0.0"
    pullPolicy: IfNotPresent
  tenancy:
    forceRLS: true                 # render-time guard: install FAILS if false in prod values
    requireNamespaceFilter: true   # C2: within-tenant namespace is a HARD, fail-closed filter
    denyOnMissingNamespace: true   # C2: missing/ambiguous namespace = deny
  topology:
    zones: ["az-a", "az-b", "az-c"]
    minReplicasFrontOfHouse: 3
    pdbMinAvailableFrontOfHouse: 2
    maxSkew: 1
  keda:
    enabled: true
    prometheusAddress: http://prometheus.monitoring:9090
  backingStores:
    postgres:   { host: contextos-pg-rw, port: 5432, sslmode: require, secret: pg-app-creds }
    redis:      { host: redis-master, port: 6379, aof: true, secret: redis-creds }
    vector:     { backend: pgvector }          # "qdrant" engages the escape hatch
    qdrant:     { enabled: false, host: "", grpcPort: 6334 }
  embeddingProvider:
    model: "BAAI/bge-small-en-v1.5"            # 384-dim, self-hosted
    dim: 384
  vllmTelemetry:
    enabled: true
    scrapeTargets: ["http://vllm-a.vllm:8000/metrics"]   # READ-ONLY; router never schedules
  networkPolicy: { defaultDeny: true }
  podSecurity:
    runAsNonRoot: true
    readOnlyRootFilesystem: true
    seccompProfile: RuntimeDefault

gateway:
  enabled: true
  replicas: 3
  resources: { requests: { cpu: "700m", memory: "512Mi" }, limits: { cpu: "2", memory: "1Gi" } }
  scaling: { signal: qps, threshold: 800, min: 3, max: 40 }
  graceful: { terminationGracePeriodSeconds: 30, drainSSE: true }

assembler:
  enabled: false                   # launch: embedded in gateway. Flip true to promote (Section 3.1).
  embeddedInGateway: true
  rustKernel: false                # C14 ADR-0001 gate; PROVISIONAL Rust ContextAssembler
  resources: { requests: { cpu: "1", memory: "512Mi" } }
  scaling: { signal: qps, threshold: 1000, min: 3, max: 30 }

memory:
  enabled: true
  replicas: 3
  candidateCap: 512                # scope-boundary invariant: never builds/owns an index
  bm25FailOpen: true               # embedding down -> BM25-only, recall loss <= 12%
  scaling: { signal: qps, threshold: 700, min: 3, max: 30 }

cache:
  enabled: true
  replicas: 3
  exactTier: redis                 # <1ms p99
  semanticTier: pgvector           # 8-15ms p95
  fingerprint: coarse              # C6
  scaling: { signal: qps, threshold: 1000, min: 3, max: 20 }

router:
  enabled: true
  replicas: 3
  hardFiltersFailClosed: true      # C9
  rbacRouteAction: route           # C10: single authority for allowlist + residency
  scaling: { signal: qps, threshold: 1500, min: 3, max: 20 }

worker:
  enabled: true
  replicas: 2
  streams: ["contextos:writeback", "contextos:consolidate", "contextos:gc", "contextos:replay-seal"]
  costLedgerOutbox: fail-closed    # C12 billing-grade durable outbox
  scaling: { signal: streamDepth, stream: "contextos:writeback", pendingThreshold: 500, min: 2, max: 30 }

embedding:
  enabled: true                    # C15: ALWAYS its own Deployment
  replicas: 2                      # warm floor, never scale to zero
  model: "BAAI/bge-small-en-v1.5"
  readinessWarmupEncode: true      # gate readiness on a sentinel encode (<=15s cold start)
  scaling:
    live:  { signal: qps, threshold: 600 }
    batch: { signal: streamDepth, stream: "contextos:embed-batch", pendingThreshold: 2000 }
    min: 2
    max: 24
```

### 9.1 Resource requests, justified

Resource requests are derived from the canonical throughput figure, not guessed.
The gateway's `700m` CPU request encodes **~0.7 vCPU per 1k req/s of
proxy+assembly** — a gateway replica is sized to absorb ~1k req/s at request, ~2
vCPU at limit for burst (matching the KEDA `threshold: 800`, which keeps each
replica under its request budget with headroom). The assembler's `1` CPU request
reflects that assembly (score+MMR+knapsack over ≤512 candidates) is the
**CPU-heaviest** in-process step and the one the Rust kernel gate (C14) targets.
Memory and cache requests are smaller because their hot-path cost is dominated by
I/O to Postgres/Redis (18 ms ANN probe, < 1 ms exact cache) rather than local CPU.

---

## 10. Network Policy: Default-Deny, Tenant-Aware

The `contextos-common.netpol` template renders a **default-deny** NetworkPolicy
plus an explicit egress allowlist per service. This is a defense-in-depth layer
**under** the RLS + RBAC firewall that enforces **zero cross-tenant leakage** —
the network layer can't see tenants, but it can guarantee the gateway can only
talk to the seven internal services + Postgres + Redis, and nothing in the
cluster can reach Postgres except memory/cache/worker.

```yaml
# rendered allowlist (excerpt)
egress:
  gateway   -> [router, memory, cache, embedding, postgres, redis]
  memory    -> [embedding, postgres, redis]
  cache     -> [postgres, redis]            # exact tier Redis, semantic tier pgvector
  router    -> [postgres, prometheus]       # RBAC route check + vLLM telemetry via Prometheus
  worker    -> [postgres, redis, object-store]
  embedding -> []                           # pure compute; no egress except DNS
ingress:
  postgres  <- [memory, cache, worker, router]
  redis     <- [gateway, memory, cache, worker, embedding]
```

The embedding service has **no egress** (it is a pure CPU encoder over inputs
pushed to it) — a strong scope-boundary signal that it is an encoder, not an
inference plane reaching out to model APIs. Rejected alternative: a flat,
allow-all cluster network "because RLS already isolates tenants." Belt-and-braces:
if an RLS regression ever slips past the ≥ 10,000-probe CI gate, the network
policy still prevents a compromised stateless pod from reaching arbitrary stores.

---

## 11. Summary of Deployment Invariants

1. **One library chart (`contextos-common`)** renders PDB/HPA/NetworkPolicy/
   topology-spread/security-context once; seven subcharts inherit them — drift is
   impossible by construction.
2. **Seven Deployments**: gateway, memory, assembler (in-gateway at launch),
   cache, router, worker, embedding (always its own, C15).
3. **KEDA per-service**: gateway/router/memory/cache = QPS; worker = stream
   depth; embedding = QPS (live) + stream depth (batch).
4. **GPU-aware routing reads vLLM telemetry only** — never schedules GPUs (C13).
5. **Backing stores**: Redis (AOF) for cache/streams/working memory; Postgres 16 +
   pgvector (CloudNativePG, PITR) for relational + vectors; Qdrant escape hatch
   behind the adapter.
6. **99.9%**: ≥ 3 replicas across ≥ 3 AZ, PDB `minAvailable: 2`, topology spread
   `maxSkew: 1`.
7. **Stateless hot path** (gateway/assembler/router/memory/cache); **stateful
   durability** isolated in operator-managed Postgres and AOF-backed Redis.
```
