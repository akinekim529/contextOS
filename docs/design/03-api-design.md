# API Design

This section specifies the **public contract** of ContextOS: the Python SDK (simple and power paths), the OpenAI-compatible REST surface (`/v1/chat`), the streaming protocol (named SSE events), and the Admin/Trace/Replay APIs. The API is the load-bearing boundary of the system — everything behind it (memory tiers, the assembler, the router, the replay bundle) is an implementation detail that this contract deliberately hides. The design goal is **a two-line happy path that an application engineer can adopt in five minutes, and a fully-explicit power path that an SRE can audit line-by-line**, both serialized over the identical wire protocol so there is exactly one thing to debug.

Everything here references the canonical latency budget owned by Section 9; this section never restates a different number. Where a control decision is made (cache, route, assemble), the API exposes it as a *structured, replayable artifact* rather than an opaque log line — that is the whole point of a replay-grade system.

---

## 1. Design principles (and what we rejected)

| Principle | Decision | Rejected alternative | Why the alternative fails |
|---|---|---|---|
| **OpenAI-compatible edge** | `/v1/chat` speaks an OpenAI-shaped request/response so existing SDKs and tools point at us by changing a base URL | A bespoke "better" schema | Adoption tax kills middleware; nobody rewrites their call sites to try a proxy. We extend OpenAI's schema with a namespaced `contextos` block instead of replacing it |
| **One wire format, two ergonomics** | The Python SDK is a thin typed client over `/v1/chat`; simple and power paths emit the *same* JSON | A "fat" client SDK with logic the server lacks | Logic-in-client means two implementations drift; replay would be a lie because the SDK did work the server never saw |
| **Sync vs stream is content-negotiated, not a different route** | `Accept: application/json` -> buffered `ChatResponse`; `Accept: text/event-stream` -> SSE | Separate `/v1/chat` and `/v1/chat/stream` endpoints | Two routes double the auth/budget/replay surface and let them skew. One route, one pipeline, negotiated rendering |
| **Every decision is a first-class object** | `route`, `cache`, `assembly`, `usage` are typed fields, not free-text | Stuffing decisions into log strings | The Replay Debugger (C7) needs structured determinism; strings are not replayable |
| **Errors are typed envelopes with a `trace_id`** | One error schema everywhere, always carrying `trace_id` | HTTP status + prose body | An LLM gateway fails in *taxonomically distinct* ways (budget, residency, 413 token-reserve, backend 5xx). Clients must branch on a stable code, and every failure must be replay-addressable |
| **Idempotency is mandatory for writes** | `Idempotency-Key` on every `/v1/chat` call; streaming replays the materialized final response with the **original** `trace_id` and **zero** second backend call (C15) | Best-effort dedupe / client-side retry only | Streaming + retries + write-back is the classic double-charge bug. We close it at the protocol level |

---

## 2. Python SDK — the simple path

The simple path is the *entire* product for 80% of callers. Two lines. No config object, no policy, no awareness that memory, caching, routing, or compression exist.

```python
from contextos import ContextOS

ctx = ContextOS(user_id="123", tenant="acme")
response = ctx.chat("how do I deploy an LLM on Kubernetes?")

print(response.text)        # the assistant's answer (str)
print(response.trace_id)    # ULID; addresses /v1/traces/{id} and /replay
print(response.usage.cost_usd)
```

What just happened, with no ceremony, in pipeline order (auth/tenant -> cache -> retrieve -> ACL/redaction -> compression -> assembly -> routing -> adapter -> stream -> async write-back):

1. The constructor resolved `tenant="acme"` to a `tenant_id` (ULID) and opened a pooled, authenticated HTTP/2 connection. `user_id="123"` becomes the default **within-tenant namespace** (C2): a hard, fail-closed filter at the repository boundary.
2. `ctx.chat(...)` POSTed an OpenAI-shaped body to `/v1/chat` with `Accept: application/json`, a generated `Idempotency-Key`, and an implicit default `ChatOptions`.
3. The server ran the full pipeline and returned a buffered `ChatResponse`. The SDK deserialized it into a typed object.

**Defaults applied when you say nothing** (every default is explicit and documented — no "magic"):

| Field | Default | Rationale |
|---|---|---|
| `memory.scope` | `["user:123"]` within `tenant=acme` | C2: namespace is a hard filter; default is the calling user only. Never silently org-wide |
| `memory.read` / `memory.write` | `True` / `True` | The simple path is stateful by design; write-back is async and off the hot path |
| `routing.policy` | `"balanced"` | Difficulty-aware: downgrades easy queries (the 20-40% routing savings), escalates hard ones |
| `cache.mode` | `"read_write"` | Both cache tiers active; coarse fingerprint (C6) |
| `budget.max_context_tokens` | model-derived, with hard reserve | C3: router picks the model *before* final packing so the correct tokenizer enforces the reserve |
| `compression` | `"auto"` | NLI-guarded 2-4x on long blocks, >=98% fact retention; runs AFTER ACL/redaction |
| `stream` | `False` | `chat()` is buffered; `stream()` opts into SSE |

### 2.1 Constructor signature

```python
class ContextOS:
    def __init__(
        self,
        *,
        user_id: str | None = None,
        tenant: str,                          # required; resolved to tenant_id (ULID)
        base_url: str = "https://api.contextos.internal/v1",
        api_key: str | None = None,           # falls back to CONTEXTOS_API_KEY env
        timeout: float = 30.0,                # seconds, end-to-end client deadline
        max_retries: int = 2,                 # idempotent retries only (see §6.4)
        default_options: "ChatOptions | None" = None,
    ) -> None: ...

    def chat(
        self,
        message: str,
        *,
        options: "ChatOptions | None" = None,  # per-call override of defaults
        idempotency_key: str | None = None,    # auto-generated ULID if omitted
    ) -> "ChatResponse": ...

    def stream(
        self,
        message: str,
        *,
        options: "ChatOptions | None" = None,
        idempotency_key: str | None = None,
    ) -> "Iterator[StreamEvent]": ...

    async def achat(self, message: str, **kw) -> "ChatResponse": ...
    def astream(self, message: str, **kw) -> "AsyncIterator[StreamEvent]": ...
```

We ship **sync and async twins** (`chat`/`achat`, `stream`/`astream`). Rejected: async-only (forces a `asyncio.run` wart into every notebook and script and alienates the synchronous majority of integrators). Rejected: sync-only with a thread pool (silently serializes concurrency and lies about it under load). The sync client wraps the async core with a private event loop; there is one implementation, two faces.

### 2.2 Idempotency-Key lifecycle: one key per logical call, reused across retries

The SDK generates **exactly one `Idempotency-Key` per logical `chat()`/`stream()` call** and **reuses that same key across all internal retry attempts** (`max_retries=2`, so up to 3 wire attempts total). The key is a **ULID** minted once at call entry — *per call, never per attempt*. This is the load-bearing detail that makes retries dedupe against the server idempotency state machine (§5.5): a retried attempt must present the *same* key so the server recognizes the `(tenant_id, Idempotency-Key)` row and replays the committed response instead of re-invoking the backend.

Op-sequence inside a single `chat()` call (3 attempts max, one key throughout):

```python
def chat(self, message, *, options=None, idempotency_key=None):
    # ONE key per logical call, minted once. NOT regenerated on retry.
    key = idempotency_key or new_ulid()        # e.g. "01J9F2K3...ULID"
    body = self._build_body(message, options)  # body hash is stable => same key is safe
    attempt = 0
    while True:
        try:
            return self._post("/v1/chat", body, headers={
                "Idempotency-Key": key,         # SAME key on attempt 0, 1, 2
                "Accept": "application/json",
            })
        except RetriableError as e:             # 429 / 503 / 504 only (retriable=true)
            attempt += 1
            if attempt > self.max_retries:      # max_retries=2 => 3 total attempts
                raise
            sleep(backoff(attempt))             # full-jitter exponential backoff
            # loop re-POSTs with the IDENTICAL key -> server dedupes (C15)
```

Worked wire trace — first attempt commits server-side but the response is lost in transit, second attempt replays it:

```text
attempt 0: POST /v1/chat  Idempotency-Key: 01J9F2K3...  -> server commits, response dropped (504 at proxy)
attempt 1: POST /v1/chat  Idempotency-Key: 01J9F2K3...  -> idem.lookup HIT -> replay final ChatResponse, SAME trace_id, ZERO backend call
```

Rejected alternative: **a fresh key per attempt** (e.g. `key = new_ulid()` inside the retry loop). It breaks dedupe entirely — each attempt presents a key the server has never seen, so attempt 1 re-executes the full pipeline and re-invokes the backend, producing the canonical double-charge-and-double-write-back bug §5.5 exists to prevent. The key is a property of the *intent to send this body once*, not of any individual network attempt; it changes only when `message` or `options` change (a genuinely different logical call).

---

## 3. Python SDK — the power path

The power path makes every default from §2 explicit and overridable. It is the same call, parameterized by a frozen, validated `ChatOptions` dataclass. Nothing here is reachable that the REST API cannot also express — `ChatOptions` *is* the `contextos` request block, typed.

### 3.1 `ChatOptions` and its components

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

# ---- Memory ----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MemoryScope:
    """C2: within-tenant namespace is a HARD, fail-closed filter.
    `include` entries are namespace selectors evaluated at the repository
    boundary against tenant_id. `shared_org` requires an RBACPolicy grant
    (action='read' on the shared namespace) or the request is DENIED."""
    include: tuple[str, ...] = ("user:self",)   # e.g. ("user:123", "project:checkout")
    shared_org: bool = False                    # opt-in; RBAC-gated (C2)
    read: bool = True
    write: bool = True
    recency_half_life_days: float = 30.0        # memory-decay; ORTHOGONAL to assembler
                                                # lost-in-the-middle ordering (C1)
    max_candidates: int = 512                   # HARD cap into assembly (invariant)

# ---- Routing ---------------------------------------------------------------

class RoutingPolicy(str, Enum):
    CHEAP    = "cheap"      # always smallest capable model
    BALANCED = "balanced"  # difficulty-aware (default)
    QUALITY  = "quality"   # always strongest in allowed pool
    PINNED   = "pinned"    # caller names model_id; still subject to hard filters

@dataclass(frozen=True, slots=True)
class RoutingOptions:
    policy: RoutingPolicy = RoutingPolicy.BALANCED
    model_id: str | None = None                 # required iff policy == PINNED
    residency: str | None = None                # e.g. "eu"; HARD filter, fail-closed (C9)
    max_cost_usd: float | None = None           # per-request hard budget ceiling

# ---- Budget / packing ------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BudgetOptions:
    max_context_tokens: int | None = None       # None => model-derived
    reserve_output_tokens: int = 1024           # HARD reserve; tokenizer-correct (C3)
    on_overflow: Literal["compress", "drop_lowest", "fail"] = "compress"
    tokenization_margin: float = 0.08           # C3: conservative pre-route estimate margin

# ---- Caching ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CacheOptions:
    mode: Literal["off", "read", "write", "read_write"] = "read_write"
    exact: bool = True                          # Redis exact-hash tier (<1ms p99)
    semantic: bool = True                       # pgvector/Qdrant ANN tier (8-15ms p95)
    semantic_threshold: float = 0.92            # cosine sim floor for a semantic hit

# ---- Compression -----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CompressionOptions:
    mode: Literal["off", "auto", "aggressive"] = "auto"
    min_fact_retention: float = 0.98            # NLI-guarded floor
    # NOTE: compression ALWAYS runs after ACL/redaction (pipeline invariant)

# ---- Top-level -------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ChatOptions:
    memory: MemoryScope = field(default_factory=MemoryScope)
    routing: RoutingOptions = field(default_factory=RoutingOptions)
    budget: BudgetOptions = field(default_factory=BudgetOptions)
    cache: CacheOptions = field(default_factory=CacheOptions)
    compression: CompressionOptions = field(default_factory=CompressionOptions)
    system_prompt: str | None = None
    system_prompt_version: str = "default"      # part of the coarse cache fingerprint (C6)
    metadata: dict[str, str] = field(default_factory=dict)  # echoed into the trace
    temperature: float = 0.7
    max_tokens: int | None = None               # output cap; <= reserve_output_tokens
```

`ChatOptions` is `frozen=True, slots=True` on purpose: an options object is part of the cache fingerprint and the replay bundle. It must be **hashable and immutable** so the same options produce the same fingerprint deterministically. Rejected: a mutable `dict` of kwargs (un-hashable, untyped, and impossible to validate or replay).

#### Why these numeric defaults (each names a rejected alternative)

| Default | Value | Why this value | Rejected alternative + why it fails |
|---|---|---|---|
| `cache.semantic_threshold` | `0.92` cosine | Sweet spot on the bge-small-en-v1.5 (384-dim) embedding geometry: high enough that a hit is genuinely the same intent, low enough to keep the semantic tier inside the canonical 25-45% cache hit-ratio band | `< 0.90` admits **false-positive hits** — near-but-different queries collide and the cache serves a stale/wrong answer (a correctness bug, the worst cache failure). `> 0.95` collapses the hit-rate — only near-verbatim paraphrases match, so the semantic tier (8-15ms p95) earns almost nothing over the exact tier and the 40-65% token-savings target slips. The cache section owns the authoritative tuning; this is the SDK default |
| `memory.recency_half_life_days` | `30.0` | One month half-life balances "remember durable user facts" against "decay stale context"; orthogonal to assembler lost-in-the-middle ordering (C1) | A very short half-life (`~7`) over-forgets durable preferences (re-asking the user facts they already gave); a very long one (`~180`) lets superseded facts outrank current ones at retrieval. 30d is the per-call default; long-lived projects override down (the §3.2 example uses `14.0`) |
| `budget.tokenization_margin` | `0.08` (C3) | Conservative 8% headroom on the **pre-route** token estimate so the post-route, tokenizer-correct re-validation rarely trips the hard output reserve and returns `413` (§4.3) | `0` (trust the pre-route estimate) under-counts because the final model's tokenizer differs from the estimator, overflowing the reserve and forcing `413`/extra compression passes. A large margin (`~0.20`) wastes context budget on phantom headroom, shrinking how much memory can be packed and degrading answer quality. `0.08` is the canonical C3 margin |

These three defaults are the SDK's surface mirror of values whose authority lives in the cache and budget sections; this section sets the *client default* and defers final tuning there, but the numbers above are canonical and must not be contradicted.

### 3.2 Complete power-path example

```python
from contextos import (
    ContextOS, ChatOptions, MemoryScope, RoutingOptions,
    RoutingPolicy, BudgetOptions, CacheOptions, CompressionOptions,
)

ctx = ContextOS(user_id="123", tenant="acme")

opts = ChatOptions(
    memory=MemoryScope(
        include=("user:123", "project:k8s-platform"),  # two namespaces, both hard-filtered
        shared_org=False,                               # no org-wide read (would need RBAC grant)
        read=True,
        write=True,
        recency_half_life_days=14.0,                    # fresher bias for fast-moving project
        max_candidates=512,                             # the invariant cap
    ),
    routing=RoutingOptions(
        policy=RoutingPolicy.BALANCED,                  # difficulty-aware downgrade/escalate
        residency="eu",                                 # HARD, fail-closed (C9); never bypassed
        max_cost_usd=0.04,                              # per-request ceiling
    ),
    budget=BudgetOptions(
        max_context_tokens=24_000,
        reserve_output_tokens=2048,                     # tokenizer-correct reserve (C3)
        on_overflow="compress",                         # 2-4x, NLI-guarded
        tokenization_margin=0.08,
    ),
    cache=CacheOptions(
        mode="read_write",
        exact=True, semantic=True,
        semantic_threshold=0.94,                        # stricter; fewer false-positive hits
    ),
    compression=CompressionOptions(mode="auto", min_fact_retention=0.98),
    system_prompt="You are a senior platform engineer. Be precise and cite tradeoffs.",
    system_prompt_version="platform-v3",                # bumps cache fingerprint (C6)
    metadata={"feature": "deploy-assistant", "ab_bucket": "B"},
    temperature=0.3,
)

response = ctx.chat(
    "how do I deploy an LLM on Kubernetes?",
    options=opts,
    idempotency_key="01J9...ULID",   # caller-supplied; safe to retry
)

# The response exposes every control decision as a typed field:
print(response.text)
print(response.route.model_id, response.route.reason)     # e.g. "BALANCED: difficulty=0.71 -> mid-tier"
print(response.cache.status)                              # "miss" | "exact_hit" | "semantic_hit"
print(response.assembly.candidates_in, response.assembly.tokens_packed)
print(response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.cost_usd)
print(response.trace_id)                                  # -> /v1/traces/{trace_id}/replay
```

### 3.3 `ChatResponse` schema (typed)

```python
@dataclass(frozen=True, slots=True)
class RouteDecision:
    model_id: str
    backend: str                     # adapter id, e.g. "vllm-eu-1"
    policy: str                      # echoes RoutingPolicy
    reason: str                      # human-readable + machine-stable prefix
    residency: str | None
    fell_open_to_static: bool        # C9: True if optimization signals were unavailable

@dataclass(frozen=True, slots=True)
class CacheDecision:
    status: Literal["miss", "exact_hit", "semantic_hit", "bypass_private"]
    tier: Literal["none", "exact", "semantic"]
    fingerprint: str                 # coarse signature (C6)
    similarity: float | None         # set only for semantic_hit

@dataclass(frozen=True, slots=True)
class AssemblyDecision:
    candidates_in: int               # <= 512
    candidates_packed: int
    tokens_packed: int
    tokens_reserved_output: int
    compressed_blocks: int
    compression_ratio: float | None  # e.g. 3.1 means 3.1x reduction on compressed blocks

@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    cost_breakdown: dict[str, float] # {"model": .., "embedding": .., "compression_nli": ..}

@dataclass(frozen=True, slots=True)
class ChatResponse:
    text: str
    finish_reason: Literal["stop", "length", "content_filter", "client_abort"]
    trace_id: str                    # ULID
    created_at: str                  # RFC-3339 UTC
    route: RouteDecision
    cache: CacheDecision
    assembly: AssemblyDecision
    usage: Usage
    replayable: bool                 # True unless memory-private-grounded & non-cacheable
```

`cost_breakdown` is non-negotiable: memory consolidation, query embedding, and NLI-guarded compression each spend money, and per the scope-boundary invariants those costs **enter the budget ledger**. The API surfaces them so the caller can see that a "cheap" routed answer still cost 6 ms of embedding and a fraction of a cent of NLI inference.

**Where do the semantic-cache and ANN-probe costs go?** They are **folded into the `embedding` key**, not split into a separate `semantic_cache` / `ann_probe` key. The rationale is that the semantic-cache lookup and the memory-retrieval ANN probe are *driven by the same artifact* — the one query embedding (`~6ms`, bge-small-en-v1.5, 384-dim) computed once per request and reused for both the semantic-cache probe (8-15ms p95) and the pgvector HNSW retrieval probe (18ms p95). The dollar cost that hits the ledger is the **embedding inference**; the ANN probe itself is a local index scan with negligible *marginal* monetary cost (it consumes latency budget — surfaced per-stage in §6.4 as the `cache` and `retrieval` rows — not a separate billable line item). So `embedding` is the honest, single home for "what the query-vector machinery cost."

Rejected alternative: **add a distinct `cost_breakdown["semantic_cache"]` (or `"ann_probe"`) key.** It would (1) double-count — the same `~6ms` embedding underlies both the cache probe and retrieval, so attributing a separate `semantic_cache` charge either splits one cost arbitrarily or invents a second one; (2) break cross-section assumption #3, which locks the vocabulary to `{model, embedding, compression_nli}` extended *only additively* — every client parser and the §5.4 partial-cost record would have to change for a line that is structurally `~0` in dollars; and (3) confuse the latency/cost split — ANN-probe expense is a *latency* concern (owned by Section 9's p95 budget and the §6.4 per-stage `p_ms`), not a monetary one. If a future deployment runs a *metered* managed vector service whose probes carry a real per-query price, the vocabulary is extended additively with a `vector_probe` key at that time; until then, folding into `embedding` is the correct, non-double-counting choice.

---

## 4. REST API — `POST /v1/chat`

The SDK is sugar; this is the contract. The route is OpenAI-compatible at the top level with a namespaced `contextos` extension block. Sync vs streaming is chosen by `Accept`, not by a different path (§1).

### 4.1 Request

```
POST /v1/chat HTTP/2
Authorization: Bearer <api_key>
Content-Type: application/json
Accept: application/json            # or: text/event-stream  (negotiates streaming)
Idempotency-Key: 01J9...ULID        # required; dedupes writes & backend calls
X-Tenant-Id: acme                   # optional; may also derive from API key
```

```json
{
  "messages": [
    { "role": "user", "content": "how do I deploy an LLM on Kubernetes?" }
  ],
  "stream": false,
  "temperature": 0.3,
  "contextos": {
    "user_id": "123",
    "memory": {
      "include": ["user:123", "project:k8s-platform"],
      "shared_org": false,
      "read": true, "write": true,
      "recency_half_life_days": 14.0,
      "max_candidates": 512
    },
    "routing": { "policy": "balanced", "residency": "eu", "max_cost_usd": 0.04 },
    "budget": {
      "max_context_tokens": 24000,
      "reserve_output_tokens": 2048,
      "on_overflow": "compress",
      "tokenization_margin": 0.08
    },
    "cache": { "mode": "read_write", "exact": true, "semantic": true, "semantic_threshold": 0.94 },
    "compression": { "mode": "auto", "min_fact_retention": 0.98 },
    "system_prompt_version": "platform-v3",
    "metadata": { "feature": "deploy-assistant", "ab_bucket": "B" }
  }
}
```

Two design choices worth defending:

- **`stream` in the body AND `Accept` negotiation.** If they disagree, `Accept` wins and the server emits a `decision` warning event / response header `X-ContextOS-Stream-Override: accept`. We honor `Accept` because content negotiation is the HTTP-native mechanism intermediaries (proxies, gateways) already understand. Rejected: trusting only the body flag — breaks for clients behind buffering proxies that strip SSE unless `Accept` advertises it.
- **`X-Tenant-Id` is optional and *subordinate* to the API key.** If the key is tenant-scoped and the header disagrees, the request is rejected `403 tenant_mismatch` (fail-closed). Rejected: header-as-source-of-truth — trivially spoofable cross-tenant escalation. tenant_id is the partition key on every row (canonical); it cannot be set by an untrusted header alone.

### 4.2 Synchronous response (`Accept: application/json`)

`200 OK`:

```json
{
  "id": "01J9...ULID",
  "object": "chat.completion",
  "created": "2026-06-28T14:03:11.482Z",
  "model": "mid-tier-eu",
  "choices": [
    { "index": 0, "finish_reason": "stop",
      "message": { "role": "assistant", "content": "Deploying an LLM on Kubernetes..." } }
  ],
  "usage": {
    "prompt_tokens": 1840, "completion_tokens": 612, "total_tokens": 2452,
    "cost_usd": 0.0123,
    "cost_breakdown": { "model": 0.0117, "embedding": 0.0003, "compression_nli": 0.0003 }
  },
  "contextos": {
    "trace_id": "01J9...ULID",
    "replayable": true,
    "cache":    { "status": "miss", "tier": "none", "fingerprint": "c6:8f3a...", "similarity": null },
    "route":    { "model_id": "mid-tier-eu", "backend": "vllm-eu-1", "policy": "balanced",
                  "reason": "BALANCED: difficulty=0.71 -> mid-tier", "residency": "eu",
                  "fell_open_to_static": false },
    "assembly": { "candidates_in": 312, "candidates_packed": 47, "tokens_packed": 1840,
                  "tokens_reserved_output": 2048, "compressed_blocks": 5, "compression_ratio": 3.1 }
  }
}
```

`trace_id == id`: the response id *is* the replay handle. There is no separate correlation id to lose.

### 4.3 Status codes

| Code | Meaning | Notes |
|---|---|---|
| `200` | Success (sync or streaming established) | SSE: `200` then the event stream; a mid-stream failure is an `error` event, not a new status |
| `400` | Malformed request | Schema/JSON errors; typed envelope |
| `401` | Missing/invalid API key | |
| `403` | Authz failure | `tenant_mismatch`, `namespace_denied` (C2), `residency_denied` (C9), `model_not_allowed` (C10) |
| `409` | Idempotency conflict | Same `Idempotency-Key`, *different* request body hash |
| `413` | Token reserve unsatisfiable | C3: post-route re-validation could not honor the hard output reserve and `on_overflow != "compress"`, or compression still overflowed |
| `422` | Semantically invalid options | e.g. `policy=PINNED` without `model_id`; `max_tokens > reserve_output_tokens` |
| `429` | Rate / budget exhausted | `Retry-After` header; distinguishes per-tenant rate limit vs `max_cost_usd` budget |
| `499` | Client closed request before server terminal | C8: write-back **discarded**; partial cost attributed (see §5.4) |
| `503` | No safe backend satisfies hard filters | C9: even the safe-default pool failed residency/allowlist; we fail closed, never relax residency |
| `504` | Backend deadline exceeded | ContextOS-side timeout on the adapter call |

**Why `499` (client closed request) and not `408` (request timeout) for an abort.** A client TCP close before the server terminal is the **client** withdrawing, not the server timing out — `499` (nginx-origin, widely understood) names exactly that: *the client closed the connection while the server was still working*. Rejected: `408 Request Timeout`. `408` has the wrong semantics — RFC 7231 §6.5.7 defines it as "the **server** did not receive a complete request message within the time it was prepared to wait," i.e. a *slow or stalled inbound request*. In our abort case the request was received whole and the server was mid-generation; emitting `408` would falsely blame the server's input-wait path, mislead retry logic (a `408` invites an immediate identical retry of an *unsent* request, whereas a `499` correctly signals "you hung up on a request that may have committed — retry with the **same** `Idempotency-Key` per §5.5"), and pollute the latency table's `adapter`-stage attribution. `499` keeps the abort cleanly attributed to the client and routes it to the §5.4 partial-cost path. (`504` remains the distinct *server-side* deadline on the backend adapter — server timeout, not client withdrawal.)

### 4.4 Typed error envelope

Every non-2xx (and every mid-stream `error` event) carries this exact shape:

```json
{
  "error": {
    "type": "residency_denied",
    "code": "ROUTE_RESIDENCY_DENIED",
    "message": "No backend in residency 'eu' is allowed for model policy 'balanced'.",
    "status": 403,
    "trace_id": "01J9...ULID",
    "stage": "routing",
    "retriable": false,
    "details": {
      "required_residency": "eu",
      "candidate_backends_evaluated": 4,
      "fell_open_to_static": false
    }
  }
}
```

- `trace_id` is **always present**, even on `400`/`401`, so every failure is replay-addressable. A failed request still produces a (partial) trace bundle up to the failing stage.
- `code` is a stable, screaming-snake-case enum (clients branch on it); `type` is the lower-snake category; `message` is human prose that may change.
- `stage` names the pipeline stage that failed (`auth`, `cache`, `retrieval`, `acl`, `compression`, `assembly`, `routing`, `adapter`, `write_back`) — directly mappable to the latency table in Section 9 and to the replay stage list.
- `retriable` tells the SDK whether `max_retries` applies. `429`/`503`/`504` are retriable with backoff; `403`/`413`/`422` are not.

---

## 5. Streaming (SSE)

When `Accept: text/event-stream`, `/v1/chat` returns `200` and an event stream. We use **named SSE events**, not anonymous data frames, so a consumer can `switch` on `event:` without parsing each `data:` payload to discover its kind. Rejected: a single `data:`-only OpenAI-style delta stream — it conflates tokens, control decisions, usage, and errors into one untyped channel, which is exactly the opacity ContextOS exists to eliminate. Rejected: WebSockets — bidirectional framing we do not need, worse proxy/HTTP-cache interop, and no content negotiation story.

### 5.1 Named events

| `event:` | When | `data:` payload |
|---|---|---|
| `decision` | Emitted **before** the first token, once per major control decision (cache, route, assembly) | A `CacheDecision` / `RouteDecision` / `AssemblyDecision` object, tagged with `"kind"` |
| `token` | Per generated chunk | `{ "delta": "...", "index": 0 }` |
| `usage` | Once, immediately before `done` | The `Usage` object (final token counts + `cost_breakdown`) |
| `done` | Terminal success | `{ "finish_reason": "stop", "trace_id": "...", "replayable": true }` |
| `error` | Terminal failure (mid-stream) | The typed error envelope (§4.4) |

The ordering contract: zero or more `decision` events -> one or more `token` events -> exactly one `usage` -> exactly one terminal (`done` **xor** `error`). The terminal event is the **server's** finish signal and is the linchpin of C8.

```text
event: decision
data: {"kind":"cache","status":"miss","tier":"none","fingerprint":"c6:8f3a..."}

event: decision
data: {"kind":"route","model_id":"mid-tier-eu","backend":"vllm-eu-1","reason":"BALANCED: difficulty=0.71 -> mid-tier","residency":"eu","fell_open_to_static":false}

event: decision
data: {"kind":"assembly","candidates_in":312,"candidates_packed":47,"tokens_packed":1840,"compression_ratio":3.1}

event: token
data: {"delta":"Deploying ","index":0}

event: token
data: {"delta":"an LLM on Kubernetes","index":0}

event: usage
data: {"prompt_tokens":1840,"completion_tokens":612,"total_tokens":2452,"cost_usd":0.0123,"cost_breakdown":{"model":0.0117,"embedding":0.0003,"compression_nli":0.0003}}

event: done
data: {"finish_reason":"stop","trace_id":"01J9...ULID","replayable":true}
```

Surfacing `decision` events *before* the first token is deliberate: it lets a UI render "routing to mid-tier (EU), cache miss, 47 memories packed" while the model is still thinking, and it means the control-plane decisions are observed on the wire even if the backend stalls.

### 5.2 The `StreamAccumulator` tee — streaming vs caching & memory write-back

Streaming and write-back are in tension: the client wants tokens *now*, but caching and memory consolidation need the *complete* response. We resolve this with a **tee**: a `StreamAccumulator` sits in the adapter's output path and simultaneously (a) forwards each chunk to the client SSE stream and (b) appends to an in-memory buffer. The buffer is only acted upon at the **server terminal event**.

```python
class StreamAccumulator:
    """Tees the backend token stream: forwards to the client AND buffers for
    cache write + memory write-back. Write-side actions fire ONLY on a server
    terminal (finish_reason reached). Client disconnect before terminal => discard."""

    def __init__(self, trace_id: str, ctx: RequestContext):
        self._trace_id = trace_id
        self._ctx = ctx
        self._buf: list[str] = []
        self._committed = False

    async def tee(self, backend_stream: AsyncIterator[Chunk]) -> AsyncIterator[StreamEvent]:
        try:
            async for chunk in backend_stream:
                self._buf.append(chunk.delta)
                yield StreamEvent.token(chunk.delta)             # (a) to client
            # backend produced a terminal finish_reason -> server terminal reached
            full_text = "".join(self._buf)
            yield StreamEvent.usage(self._ctx.usage)
            await self._commit(full_text)                        # (b) cache + write-back
            yield StreamEvent.done(self._trace_id, finish_reason=self._ctx.finish_reason)
        except (ClientDisconnect, asyncio.CancelledError):
            # C8: client TCP close BEFORE server terminal -> discard write-side
            await self._abort_partial()
            raise

    async def _commit(self, full_text: str) -> None:
        # C8: server finish_reason reached => COMMIT.
        # Cache write is gated by replayability (C6): private-grounded => skip cache.
        if self._ctx.replayable and self._ctx.cache_mode in ("write", "read_write"):
            await self._ctx.cache.put(self._ctx.fingerprint, full_text)
        # Memory write-back is enqueued onto Redis Streams (async plane), off hot path.
        await self._ctx.writeback.enqueue(self._trace_id, full_text)
        # Materialize the final ChatResponse under the Idempotency-Key (C15).
        await self._ctx.idem.store_final(self._ctx.idem_key, self._trace_id, full_text)
        self._committed = True

    async def _abort_partial(self) -> None:
        # No cache write, no memory write-back. Attribute partial model cost only.
        await self._ctx.cost_ledger.record_partial(
            self._trace_id, tokens=len(self._buf), reason="client_abort"
        )
```

### 5.3 C8 — client-abort semantics

The **terminal-event source decides**:

| Event | Who emitted the terminal | Outcome |
|---|---|---|
| Backend reached `finish_reason` (`stop`/`length`/`content_filter`) | **Server** | **Commit**: cache write (if replayable), memory write-back enqueued, final response materialized under the idempotency key. SSE `done` sent (or buffered for a future idempotent replay) |
| Client TCP close *before* any server terminal | **Client** | **Discard**: no cache write, no memory write-back, no materialized final response. HTTP status logged as `499`. Partial **model** cost is still attributed to the ledger (§5.4) |

This is the only correct resolution: write-back of a half-generated answer would poison memory with truncated facts and poison the cache with an incomplete response. A client that hangs up gets billed for the tokens the backend already produced (we cannot un-spend them) but contributes nothing to durable state.

### 5.4 Partial-cost attribution

On a `499` client-abort, the cost ledger records:

```json
{
  "trace_id": "01J9...ULID",
  "kind": "partial",
  "reason": "client_abort",
  "prompt_tokens": 1840,
  "completion_tokens_emitted": 138,
  "completion_tokens_billed": 138,
  "cost_usd": 0.0041,
  "cost_breakdown": { "model": 0.0035, "embedding": 0.0003, "compression_nli": 0.0003 },
  "committed": false
}
```

`completion_tokens_billed == completion_tokens_emitted`: the caller pays for exactly what the backend generated before the abort, plus the already-spent control costs (embedding + compression NLI were incurred *before* the first token, so they are non-refundable). `committed: false` flags that this trace produced **no** durable memory/cache effect. Billing-grade cost records go through the **fail-closed durable outbox** (C12) — a partial cost is still a real cost and must not be lost even though the request "failed."

### 5.5 C15 — streaming idempotency

The `Idempotency-Key` header is mandatory. On a **retry of a key whose original request committed** (server reached terminal), the server returns the **materialized final `ChatResponse`** with the **original `trace_id`** and makes **zero second backend call**.

```text
Client                         ContextOS                       Backend
  | POST /v1/chat (Idem=K, SSE) ---->|                              |
  |                                  | route + assemble + invoke -->|
  |   token, token, ... done <-------|<------ stream ---------------|
  |   (commit: store_final(K))       |                              |
  |                                  |                              |
  | -- connection dropped at client; client retries with same K -- |
  | POST /v1/chat (Idem=K, SSE) ---->|                              |
  |                                  | idem.lookup(K) -> HIT        |   <-- NO backend call
  |   <-- replays materialized final ChatResponse, SAME trace_id    |
```

Idempotency state machine, keyed by `(tenant_id, Idempotency-Key)`:

| Stored state | New request, **same** body hash | New request, **different** body hash |
|---|---|---|
| *absent* | Execute pipeline; lock the key `in_flight` | (n/a) |
| `in_flight` | `409 idempotency_in_flight`, `Retry-After: 1` | `409 idempotency_conflict` |
| `committed` | **Replay** materialized `ChatResponse` (original `trace_id`), zero backend call (C15). For SSE retries, replay the buffered response as a synthesized stream (`decision`* -> `token`* -> `usage` -> `done`) | `409 idempotency_conflict` |
| `aborted` (client-abort, never committed) | Re-execute (it was discarded); lock `in_flight` | `409 idempotency_conflict` |

A streaming retry of a committed key replays the *materialized* response synthesized back into SSE frames — the client cannot tell it from a fresh stream except that `trace_id` is identical and `cache.status` may read `idempotent_replay`. Idempotency records are retained 24h (TTL) then GC'd; this window covers retry storms without unbounded growth. Rejected: no idempotency / client-only retry — the canonical double-charge-on-retry bug for any streaming gateway with write-back.

---

## 6. Admin & Trace/Replay API — `/v1/admin/*`, `/v1/traces/*`

Admin routes require an API key with `action='admin'` (C10 enum) and are **always** tenant-scoped — there is no cross-tenant admin surface (cross-tenant leakage canonical = 0). Every admin mutation is itself traced.

### 6.1 Memory inspection

```
GET  /v1/admin/memory?namespace=project:k8s-platform&q=kubernetes&limit=20
GET  /v1/admin/memory/{memory_id}
DELETE /v1/admin/memory/{memory_id}        # RTBF single-record; tombstone + GC (C11)
POST /v1/admin/memory/forget               # RTBF by subject; crypto-shred the DEK (C11)
```

`GET /v1/admin/memory` returns candidates with **raw per-modality scores** (C1) — the admin sees what the Memory Engine returns *before* the assembler re-ranks, which is essential for debugging "why was this memory retrieved?":

```json
{
  "namespace": "project:k8s-platform",
  "tenant_id": "acme",
  "results": [
    { "memory_id": "01J9...ULID", "tier": "semantic", "created_at": "2026-05-02T09:11:04Z",
      "scores": { "vector_cosine": 0.83, "bm25": 11.4 }, "recency_weight": 0.61,
      "text": "Our prod cluster runs GPU node pool g5.12xlarge ...",
      "namespace": "project:k8s-platform" }
  ]
}
```

`POST /v1/admin/memory/forget` implements C11 right-to-be-forgotten: it writes a tombstone, returns immediately, and an **idempotent GC sweep** crypto-shreds the per-subject DEK. Because embeddings are within crypto-shred scope (the vector payload and id are encrypted under the per-subject DEK), shredding the DEK renders the vector irrecoverable across *both* Postgres rows and the pgvector/Qdrant index — no separate vector-deletion path can be forgotten or skewed.

```json
// POST /v1/admin/memory/forget request
{ "subject": "user:123", "reason": "gdpr_rtbf" }
// 202 Accepted
{ "tombstone_id": "01J9...ULID", "subject": "user:123", "dek_shred": "scheduled",
  "sweep_eta_seconds": 60, "trace_id": "01J9...ULID" }
```

### 6.2 Cache inspect & purge

```
GET    /v1/admin/cache/{fingerprint}        # inspect a coarse-fingerprint entry
DELETE /v1/admin/cache?namespace=...&tier=semantic   # targeted purge
POST   /v1/admin/cache/purge                # bulk purge by selector
```

```json
// GET /v1/admin/cache/c6:8f3a...
{
  "fingerprint": "c6:8f3a...",
  "tier": "semantic",
  "components": {                       // the COARSE signature inputs (C6)
    "query_embedding_bucket": "b_0314",
    "model_id": "mid-tier-eu",
    "system_prompt_version": "platform-v3",
    "stable_fact_set_version": "facts-2026-06-21"
  },
  "hits": 142, "created_at": "2026-06-21T00:00:11Z", "last_hit_at": "2026-06-28T13:59:02Z",
  "size_bytes": 4821, "private_grounded": false
}
```

Inspect deliberately exposes the **four coarse-fingerprint components** (C6) so an operator can reason about why two queries did/did not collide. Purge is the operator's lever when `stable_fact_set_version` rolls forward and stale entries must be evicted ahead of natural TTL.

### 6.3 Policy management

```
GET  /v1/admin/policy
PUT  /v1/admin/policy/routing            # allowlist, residency, difficulty thresholds
PUT  /v1/admin/policy/rbac               # action grants: read/write/delete/admin/route/cache_read
GET  /v1/admin/policy/validate           # dry-run a (principal, resource, action) check
```

RBAC and routing policy share one authority (C10): `RoutePolicy.allowed_backends` **derives** from `check(principal, resource=model, action='route')`; there is no second policy store to drift. `PUT /v1/admin/policy/routing` validates that the **safe-default pool itself satisfies all hard filters** (C9) — a policy that would leave the safe-default pool empty under residency `eu` is rejected `422 unsafe_default_pool` rather than silently allowing a residency bypass at runtime.

```json
// GET /v1/admin/policy/validate?principal=svc:deploy-bot&resource=model:strong-eu&action=route
{ "allowed": true, "authority": "rbac", "matched_rule": "route:eu-pool",
  "residency_ok": true, "trace_id": "01J9...ULID" }
```

### 6.4 Trace querying

```
GET /v1/admin/traces?from=2026-06-28T00:00:00Z&to=2026-06-28T23:59:59Z&model_id=mid-tier-eu&min_cost_usd=0.05&limit=50
GET /v1/traces/{trace_id}
```

Trace listing reflects the C12 sampling posture: best-effort traces are tail-sampled (1-10%) but **errors and any request with `cost_usd > $0.05` are force-kept**, so `min_cost_usd=0.05` queries are always complete. `GET /v1/traces/{trace_id}` returns the full stage-by-stage record:

```json
{
  "trace_id": "01J9...ULID",
  "tenant_id": "acme",
  "created_at": "2026-06-28T14:03:11.482Z",
  "force_kept": true,
  "force_kept_reason": "cost>0.05",
  "stages": [
    { "stage": "auth",        "p_ms": 4.8,  "tenant_id": "acme", "namespace": ["user:123","project:k8s-platform"] },
    { "stage": "cache",       "p_ms": 9.6,  "decision": { "status": "miss", "tier": "none" } },
    { "stage": "retrieval",   "p_ms": 39.2, "candidates": 312, "fuse": "rrf" },
    { "stage": "acl",         "p_ms": 2.1,  "redacted_blocks": 1 },
    { "stage": "compression", "p_ms": 18.0, "ratio": 3.1, "fact_retention": 0.991 },
    { "stage": "assembly",    "p_ms": 47.5, "packed": 47, "tokens": 1840 },
    { "stage": "routing",     "p_ms": 4.9,  "model_id": "mid-tier-eu", "fell_open_to_static": false },
    { "stage": "adapter",     "p_ms": 7.8 }
  ],
  "usage": { "total_tokens": 2452, "cost_usd": 0.0612, "cost_breakdown": { "model": 0.0606, "embedding": 0.0003, "compression_nli": 0.0003 } },
  "replayable": true
}
```

Per-stage `p_ms` values are illustrative of a single request and are bounded by the authoritative p95 budget in Section 9 (e.g. assembly < 50 ms, retrieval < 100 ms, vector probe p95 18 ms); this section never asserts a different SLO number.

### 6.5 Replay — `POST /v1/traces/{trace_id}/replay` (C7)

Replay re-executes the recorded request against the **one** `ReplayResult` schema shared across the API, observability, and the killer-feature debugger (C7). The deterministic stages — **all ContextOS decisions** (cache fingerprint, retrieval candidate set, ACL/redaction, compression, assembly packing, routing decision) — must reproduce **byte-exact** from the content-addressed, per-tenant-encrypted bundle. `backend.invoke` is non-deterministic and is handled by the `live_backend` flag.

```
POST /v1/traces/{trace_id}/replay
{ "live_backend": false }     // default: assert byte-equality vs recorded output
```

```json
{
  "trace_id": "01J9...ULID",
  "replay_id": "01J9...ULID",
  "deterministic_stages": {
    "cache_fingerprint":  { "recorded": "c6:8f3a...", "replayed": "c6:8f3a...", "equal": true },
    "retrieval_candidates": { "recorded_hash": "sha256:1ab...", "replayed_hash": "sha256:1ab...", "equal": true },
    "acl_redaction":      { "recorded_hash": "sha256:77c...", "replayed_hash": "sha256:77c...", "equal": true },
    "compression":        { "recorded_hash": "sha256:90d...", "replayed_hash": "sha256:90d...", "equal": true },
    "assembly":           { "recorded_hash": "sha256:c4e...", "replayed_hash": "sha256:c4e...", "equal": true },
    "routing":            { "recorded": "mid-tier-eu", "replayed": "mid-tier-eu", "equal": true }
  },
  "all_deterministic_equal": true,
  "backend": {
    "live_backend": false,
    "output_equal": true,        // asserted ONLY for recorded-output replay (C7)
    "diff": null
  }
}
```

C7 governs `backend`:

- `live_backend=false` (default): replay asserts `output_equal` against the **recorded** output bytes. This is the regression-grade replay — any divergence in a deterministic stage fails the replay and is a bug in ContextOS.
- `live_backend=true`: ContextOS re-invokes the live model. Byte-equality is **not** asserted (the model is non-deterministic); the result is a **diff**, not an equality claim:

```json
{
  "backend": {
    "live_backend": true,
    "output_equal": null,
    "diff": {
      "type": "text_diff",
      "recorded_len": 612, "replayed_len": 640,
      "unified": "@@ -1 +1 @@\n-...recorded answer...\n+...fresh answer..."
    }
  }
}
```

The hard contract is: **deterministic stages are byte-exact or the replay fails**; `backend.invoke` is the *only* permitted source of divergence, and even then only under `live_backend=true`. This is what makes ContextOS "replay-grade": when a user asks "why did the model say that on June 28?", we can prove the exact context the model received, byte for byte, and isolate any difference to the model itself.

---

## 7. Cross-cutting API conventions

| Convention | Value | Reason |
|---|---|---|
| IDs | ULID | Canonical; lexicographically sortable, time-prefixed — trace listings sort by id without a secondary timestamp index |
| Timestamps | RFC-3339 UTC | Canonical; unambiguous across regions |
| Tenancy | `tenant_id` non-null on every request, row, cache key, trace, replay bundle | Canonical partition key; cross-tenant leakage = 0, enforced by FORCE RLS + the RBAC firewall and CI's >=10,000 hostile probes |
| Idempotency | `Idempotency-Key` required on `/v1/chat` | C15; the only correct way to make streaming + write-back retry-safe |
| Error envelope | `{error:{type,code,message,status,trace_id,stage,retriable,details}}` | One taxonomy everywhere; every failure replay-addressable |
| Versioning | URL-major (`/v1`), additive minor via the `contextos` block | OpenAI-shaped top level stays stable; we evolve under our namespace |
| Pagination | Opaque `cursor` + `limit` (max 200) | ULID-keyed keyset pagination; no offset scans on partitioned tables |
| Auth | `Authorization: Bearer`; key is tenant- and action-scoped (C10 enum) | Header tenant claims are subordinate to the key (fail-closed) |

---

## Cross-section assumptions (other sections must match)

1. **The response id IS the trace_id** (`chat.completion.id == contextos.trace_id == replay handle`). Observability (Section on tracing) and the Replay Debugger must treat the OpenAI `id` field as the single ULID correlation key — no separate correlation id.
2. **The `contextos` request block is the canonical serialization of `ChatOptions`.** Any section adding a knob (router, assembler, cache) must add it under `contextos.*` with the same field name as the SDK dataclass; the SDK is a typed mirror, not a superset.
3. **`cost_breakdown` keys `{model, embedding, compression_nli}` are a fixed vocabulary.** The budget-ledger / cost section must emit exactly these keys (extended only additively) so client parsers and the partial-cost record (§5.4) stay stable. Semantic-cache and ANN-probe expense is **folded into `embedding`** (one query vector drives both the cache probe and retrieval; the probe itself is a latency cost, not a billable line — see §3.3); no `semantic_cache`/`ann_probe` key exists. Any future metered managed-vector service is added additively as `vector_probe`, never by splitting `embedding`.
4. **Pipeline `stage` names are a closed enum** (`auth, cache, retrieval, acl, compression, assembly, routing, adapter, write_back`) shared by the error envelope (§4.4), trace records (§6.4), and the replay deterministic-stage list (§6.5). The Architecture and Observability sections must use these exact tokens so a `stage` value maps 1:1 from an error to a latency row to a replay assertion.
