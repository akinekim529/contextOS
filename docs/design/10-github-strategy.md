# GitHub Strategy: From Zero to 1,000 Stars

This section is a launch and growth playbook, not a vibe. ContextOS is open-source middleware in a crowded, noisy category (everyone has a "memory layer" or an "LLM gateway"). The goal of this section is mechanical: **define exactly what we ship on day zero, how the repository proves it is real in under five minutes, and the ordered sequence of launch surfaces that converts a cold visitor into a star, then into a contributor.**

The thesis: stars are a *trust* metric, and trust in infra tooling is earned by one undeniable, reproducible demo plus a repository that signals operational seriousness (CI gates, ADRs, versioned contracts) before a single feature is read. We win on the **Context Replay Debugger** — byte-exact replay of every context decision — because no competitor can answer "why did the LLM say *that*?" at the granularity we can. That is the wedge. Everything below serves that wedge.

---

## 1. The One Undeniable Feature

Pick exactly one feature to be undeniable. Diffuse positioning ("memory + caching + routing + observability") reads as a framework and dies in the comments. We lead with **the Context Replay Debugger** and let the rest of the platform be discovered.

> **The promise (README hero, verbatim):**
> **"See exactly why your LLM said that."**
> ContextOS records every context-assembly decision into a content-addressed, per-tenant-encrypted bundle, and replays it **byte-for-byte**. Set a breakpoint on the exact moment a memory was retrieved, an ACL redacted a span, a chunk was compressed, or a candidate lost the knapsack. Your RAG stack can't tell you why it built that prompt. ContextOS can.

**Why this is the wedge and not "memory" or "caching":**

| Candidate wedge | Why we reject it as the lead |
| --- | --- |
| "Memory layer" | mem0 owns the mindshare; we'd be the 2nd memory library and argue on benchmarks nobody trusts. Losing battle for attention. |
| "Semantic cache" | GPTCache already defines the category; "we cache too" is a feature bullet, not a headline. |
| "LLM gateway / router" | LiteLLM is the default; competing head-on on provider count is a treadmill we can't win in week one. |
| "Observability / tracing" | Langfuse owns dashboards; tracing alone is table stakes and undifferentiated. |
| **Context Replay Debugger** | **Nobody else does byte-exact, content-addressed, encrypted replay of *context decisions*.** It is visually demonstrable in a GIF, viscerally useful, and structurally hard to copy (it requires deterministic-stage capture per the C7 replay contract). This is the moat. |

The replay contract is the **single `ReplayResult` schema (C7)**: deterministic stages = *all* ContextOS decisions (auth/tenant, cache lookup, retrieval, ACL/redaction, compression, assembly/packing, routing); `backend.invoke` is the one non-deterministic boundary. `output_equal` is asserted **only** for recorded-output replay; `live_backend=True` yields a *diff*, never byte-equality. The demo shows exactly this distinction — it is the credibility anchor.

---

## 2. The README — Hero, Proof, Architecture, Comparison

The README is the product. A visitor decides in ~8 seconds. Structure, top to bottom:

### 2.1 Hero block

1. **Logo + one-line tagline:** *"Context middleware for LLMs. See exactly why your model said that."*
2. **The demo GIF** (autoplaying, < 6 MB, loops cleanly) — the single most important asset in the entire repository. It shows the Replay Debugger: a chat response on the left, and on the right the replay timeline scrubbing through `retrieve -> ACL/redact -> compress -> assemble -> route`, stopping on the exact candidate that was dropped by the budget knapsack, with the token accounting visible. Caption: *"Byte-exact replay of every context decision."*
3. **Badges:** CI status, codecov, Apache-2.0, PyPI version, Discord, "cross-tenant leakage: 0 (CI-gated 10k probes)".
4. **Three-line value prop**, no marketing adjectives:
   - **Owns context, not your model.** OpenAI-compatible `/v1`; point your existing client at ContextOS, change nothing else.
   - **Replay-grade observability.** Every decision is content-addressed and reproducible.
   - **Multi-tenant by construction.** Postgres `FORCE ROW LEVEL SECURITY` + RBAC firewall; **0 cross-tenant leakage**, enforced by a CI hard gate of **≥10,000 hostile second-tenant probes**.

### 2.2 The 5-minute hello-world (placed ABOVE the architecture diagram)

People star what they can run. The quickstart must be copy-pasteable, hermetic, and finish in **under five minutes on a laptop with Docker**. No GPU, no cloud account, no API key required for the local path (self-hosted BGE embedder, `BAAI/bge-small-en-v1.5`, 384-dim — the canonical default embedder).

````markdown
## Quickstart (5 minutes, no GPU, no API key)

```bash
git clone https://github.com/contextos/contextos && cd contextos
docker compose up -d        # Postgres 16 + pgvector, Redis, embedder, gateway
uv run contextos seed       # creates tenant "demo", loads a sample corpus
```

Point any OpenAI client at the gateway:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="demo-tenant-key")

resp = client.chat.completions.create(
    model="contextos-auto",                       # router picks the backend
    messages=[{"role": "user", "content": "What did we decide about the Q3 launch?"}],
)
print(resp.choices[0].message.content)
print("replay_id:", resp.model_extra["x_contextos_replay_id"])  # ULID
```

Now replay **exactly** what ContextOS did to build that prompt:

```bash
uv run contextos replay <replay_id> --open    # opens the Replay Debugger UI
```
````

Design constraints on this quickstart, enforced as decisions:

- **`docker compose up` brings the whole control plane up locally**, consistent with the architecture's "in-process at launch" stance. No Kubernetes required to evaluate. (Helm/distroless is the production path, not the eval path — forcing Helm on a first-time visitor is the #1 reason infra repos lose the star. Rejected.)
- **The backend defaults to a local stub model** so the demo runs offline with zero spend. A `--backend openai` flag is a one-line opt-in. This guarantees the quickstart never fails on a missing key or rate limit, which is the most common silent quickstart death.
- **`x_contextos_replay_id`** is surfaced on every response so the very first thing a user does after "it works" is "show me why" — funneling straight into the wedge.

### 2.3 Architecture diagram + the pipeline invariant

A single SVG showing the **pipeline ordering invariant** explicitly, because it is the conceptual spine of the product and stating it builds authority:

```
auth/tenant -> cache lookup -> retrieve candidates -> ACL/redaction
            -> compression -> assembly/packing -> routing -> adapter
            -> stream -> async write-back
```

One callout under the diagram, because reviewers on r/MachineLearning will look for exactly this: **"Compression ALWAYS runs after ACL/redaction"** — you can never compress a span a user isn't allowed to see, so redaction is upstream by invariant, not by configuration.

### 2.4 Honest scope box (builds trust by saying what we are NOT)

```
ContextOS is middleware. It is NOT an LLM, an inference engine, a vector DB,
a training system, or an agent framework. It scores/ranks pre-retrieved
candidates (<=512) in-process and never builds or owns an index.
```

This box does disproportionate work in comment sections: it preempts the "isn't this just LangChain?" dismissal by drawing the boundary before the reader does.

---

## 3. The Comparison Table (in README, linked from the blog)

Comparison tables are the highest-converting section for infra repos because the visitor is already mentally holding "I use X — why switch?". We name competitors honestly and only claim what the demo proves. We do **not** claim to replace any of them wholesale; we claim a different layer with one feature none of them have.

| Capability | **ContextOS** | mem0 | Letta | Zep | GPTCache | LiteLLM | OpenRouter | Langfuse | LangSmith | Helicone |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Primary role** | Context middleware (memory + assembly + cache + routing + replay) | Memory layer | Stateful agent server (memory + agent runtime) | Memory + temporal knowledge graph | Semantic cache | LLM gateway/router | Hosted multi-provider routing marketplace | LLM observability/tracing | LLM app eval + tracing (LangChain) | LLM observability/cost proxy |
| **Byte-exact context-decision replay** | **Yes — content-addressed, per-tenant-encrypted bundle; single `ReplayResult` schema** | No | No | No | No | No | No | No (records traces, not deterministic replay) | No (logs runs + evals, not deterministic re-derivation) | No (request logs, not replay) |
| **Context assembly under token budget** | **Yes — score+MMR+knapsack over ≤512 candidates, < 50 ms p95** | Partial (retrieval only) | Partial (agent-managed window, no budget-knapsack) | Partial (graph retrieval only) | No | No | No | No | No | No |
| **Multi-tenant isolation** | **Postgres FORCE RLS + RBAC firewall; 0 leakage, 10k-probe CI gate** | App-level | App/project-level | Per-user/session | Namespace-level | Per-key | Per-account | Project-level | Workspace/project-level | Per-key |
| **Semantic + exact two-tier cache** | **Yes — Redis exact (<1 ms p99) + pgvector/Qdrant ANN (8–15 ms p95)** | No | No | No | Yes (semantic only) | Basic | Passthrough only | No | No | Yes (exact bucket cache) |
| **Model routing (difficulty/utility/breaker)** | **Yes — fail-closed hard policy (C9), RBAC `route` action (C10)** | No | No | No | No | **Yes (broad provider matrix)** | **Yes (price/latency routing)** | No | No | Basic (load-balance/fallback) |
| **OpenAI-compatible `/v1` drop-in** | **Yes** | No | Partial | No | Partial | **Yes** | **Yes** | N/A (SDK wrap) | N/A (SDK wrap) | **Yes (base_url proxy)** |
| **Self-hosted embeddings** | **Yes — BGE, pluggable provider** | Optional | Optional | Optional | Optional | N/A | N/A | N/A | N/A | N/A |
| **RTBF / crypto-shred incl. embeddings** | **Yes — tombstone + idempotent GC, per-subject DEK (C11)** | No | No | No | No | No | No | Data deletion only | Data deletion only | Data deletion only |
| **License** | **Apache-2.0** | Apache-2.0 | Apache-2.0 | Apache-2.0 | MIT | MIT | Closed (hosted) | MIT | Closed (hosted) | Apache-2.0 |

> The five right-most columns (Letta, Zep, LangSmith, Helicone, OpenRouter) extend the four head-to-head incumbents to a 9-competitor field; the **same five functional slices** (memory, cache, routing, observability, orchestration) and the full reasoning for why each owns its slice but is structurally blind to the joint context-window problem are laid out in the competitive-landscape table of [`00-executive-summary.md`](00-executive-summary.md). The takeaway is uniform across all nine: **none reconstructs a context decision byte-for-byte**, which is the only column ContextOS leads on without qualification.

**Honest "use them together" footer** (critical — attacking incumbents loses; composing with them wins):

> ContextOS sits in front of your model. It **complements** LiteLLM (use LiteLLM as a downstream adapter target), exports OpenTelemetry spans you can ship to Langfuse, and can ingest a mem0 store behind the `MemoryProvider` interface. We replace none of them; we add the layer that records and replays *why your context looked the way it did*.

This footer is strategic: it converts potential detractors (maintainers/users of those projects) into amplifiers, because we're not threatening their adoption.

### 3.1 Inference-runtime composition: ContextOS never competes with vLLM / TGI / Ollama

The most common objection on r/LocalLLaMA and the vLLM Discord is "isn't this just another serving layer?". It is not, and the boundary is mechanical: **ContextOS runs no model weights and schedules no GPUs.** It owns the context window *upstream* of inference and hands a finished prompt to whatever runtime actually executes tokens. The three runtimes people will pair it with:

| Runtime | What it is | Why ContextOS never competes with it | How it composes |
| --- | --- | --- | --- |
| **vLLM** | High-throughput GPU inference server (PagedAttention KV-cache, continuous batching); exposes an OpenAI-compatible `/v1`. | ContextOS does no KV-cache paging, no batching, no GPU scheduling — it has zero tokens-per-second to optimize because it never decodes. | Point the backend adapter at the vLLM endpoint: `backend.base_url = "http://vllm:8000/v1"`. ContextOS assembles the prompt, vLLM decodes it, the `BackendInvocation` boundary (C7) records the call. |
| **TGI** (Text Generation Inference) | HuggingFace's Rust/Python GPU serving stack (tensor parallelism, token streaming) with an OpenAI-compatible route. | Same boundary — TGI owns the decode loop and GPU memory; ContextOS owns retrieval/ACL/compression/assembly/routing *before* the request reaches TGI. | Identical adapter target: `backend.base_url = "http://tgi:8080/v1"`; streaming tokens flow back through the gateway unchanged. |
| **Ollama** | Local single-box runtime (GGUF/llama.cpp) for laptops/edge; OpenAI-compatible `/v1` on `:11434`. | ContextOS does no quantization, no model pull, no llama.cpp execution; it is the control plane, Ollama is the engine. | `backend.base_url = "http://localhost:11434/v1"` — this is exactly the "no API key, fully local" path the quickstart's stub backend stands in for; swapping the stub for Ollama is a one-line config change. |

Because all three already speak OpenAI-compatible `/v1`, the adapter is the **same `OpenAICompatBackend`** in every case — only the `base_url` changes. This is the structural reason ContextOS composes rather than competes: it is a client of the runtime, never a replacement for it.

---

## Migration / Adoption Path (<30 min)

The single biggest adoption blocker for middleware is "I have to rewrite my app." ContextOS sits behind the **OpenAI-compatible `/v1`** surface specifically so the migration is a *configuration* change, not a *code* change. The drop-in compatibility is load-bearing — it is why the migration fits in under 30 minutes — and every step below preserves it.

### Case 1 — raw OpenAI-API app: change `base_url` only

If you already call the OpenAI SDK, the migration is a one-line diff. Nothing else changes — same `messages`, same streaming, same response shape — because the ContextOS gateway speaks OpenAI-compatible `/v1`:

```diff
  from openai import OpenAI

- client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
+ client = OpenAI(
+     base_url="http://localhost:8080/v1",   # the ContextOS gateway
+     api_key="demo-tenant-key",             # your ContextOS tenant key
+ )

  resp = client.chat.completions.create(
      model="contextos-auto",                # router picks the backend; "gpt-4o" etc. still works
      messages=[{"role": "user", "content": "What did we decide about the Q3 launch?"}],
  )
+ print(resp.model_extra["x_contextos_replay_id"])   # NEW: byte-exact replay handle, ULID
```

The only *added* surface is `x_contextos_replay_id` on the response — a pure addition that an existing app simply ignores until it wants replay.

### Case 2 — LangChain app: point `ChatOpenAI` at the gateway

LangChain's `ChatOpenAI` is itself an OpenAI-compatible client, so the same one-line change applies — no adapter, no chain rewrite:

```diff
  from langchain_openai import ChatOpenAI

- llm = ChatOpenAI(model="gpt-4o", api_key=OPENAI_API_KEY)
+ llm = ChatOpenAI(
+     model="contextos-auto",
+     base_url="http://localhost:8080/v1",   # ContextOS gateway
+     api_key="demo-tenant-key",
+ )
  # every chain / agent built on `llm` now routes through ContextOS unchanged
```

If you instead want explicit access to the ContextOS extras (replay id, budget ledger, residency hints) inside a chain, wrap once with the thin adapter rather than touching call sites:

```python
# before: vanilla LangChain LLM
llm = ChatOpenAI(model="gpt-4o", api_key=OPENAI_API_KEY)

# after: same object, plus ContextOS metadata threaded into each Generation
from contextos.langchain import ContextOSChat
llm = ContextOSChat(base_url="http://localhost:8080/v1", api_key="demo-tenant-key")
# llm.invoke(...).response_metadata["x_contextos_replay_id"]  -> ULID
```

### Timed onboarding checklist (justifies the <30-minute bound)

| Step | Action | Budget | Why it fits |
| --- | --- | --- | --- |
| 1. Install | `git clone … && docker compose up -d` (Postgres+pgvector, Redis, embedder, gateway, stub backend) | ~8 min | Dominated by image pulls on a cold cache; warm cache is ~90 s. No GPU, no cloud account, no key (matches the §2.2 quickstart). |
| 2. Seed | `uv run contextos seed --tenant demo` | ~1 min | Creates the tenant + sample corpus; one command. |
| 3. Point client | apply the one-line `base_url` diff above (Case 1 or 2) | ~5 min | A single edit in one file; no call-site or chain rewrite. |
| 4. First request | re-run your existing app; confirm a normal completion + a surfaced `x_contextos_replay_id` | ~5 min | Same request/response shape, so existing assertions still pass. |
| 5. First replay | `uv run contextos replay <replay_id> --open` | ~5 min | The wedge moment — you see *why* the prompt was built that way. |
| **Total** | | **~24 min** | Comfortably inside the **<30-minute** bound with slack for a cold image cache. |

**Rejected alternative — ship a custom ContextOS SDK and require a migration to it.** A bespoke SDK (`contextos.Client(...)` with its own method names) would **break drop-in compatibility**: every call site, every LangChain integration, and every third-party tool that already speaks OpenAI's `/v1` would need rewriting, the migration would jump from one line to a multi-day refactor, and we would forfeit the entire ecosystem of OpenAI-compatible clients (LangChain, LlamaIndex, the official SDKs in every language). The whole adoption thesis is "change `base_url`, change nothing else"; a custom SDK is the one decision that would invalidate it, so it is rejected.

---

## 4. Documentation as a Credibility Signal: ADR-per-Decision

Every meaningful architecture decision lives under `docs/adr/` as a numbered Architecture Decision Record. This is not bureaucracy — for an infra project, **a populated `docs/adr/` directory is the single strongest signal that the maintainers are serious systems engineers**, and reviewers on Lobste.rs/HN explicitly look for it.

Format: [MADR](https://adr.github.io/madr/)-style, one decision per file, `NNNN-kebab-title.md`, with a `Status`, `Context`, `Decision`, `Consequences`, and a mandatory **"Rejected Alternatives"** block. The rule, enforced in PR review: **no significant technology or algorithm choice merges without an ADR that names at least one rejected alternative and why it fails.**

Day-zero ADRs (the locked architecture maps 1:1 to these):

| ADR | Title | Rejected alternative named |
| --- | --- | --- |
| 0001 | **Rust/PyO3 hot-path kernel is PROVISIONAL, behind a benchmark gate** | "Rust everything now" — premature; Python asyncio+uvloop meets the < 50 ms assembly budget at launch scale (see §5.4, C14). |
| 0002 | Postgres 16 + pgvector co-located behind a `VectorStore` adapter | Standalone Qdrant from day one — operational overhead before we have ≥5M vectors/tenant; pgvector HNSW gives **p95 = 18 ms** ANN at launch scale. Qdrant is the escape hatch ≤25 ms beyond. |
| 0003 | Redis Streams + custom asyncio consumer as the async plane (doubles as replay log) | Kafka — heavyweight ops for our volume; we'd run a cluster to move a few thousand events/s. |
| 0004 | `FORCE ROW LEVEL SECURITY` + RBAC firewall for tenant isolation | App-layer `WHERE tenant_id=` filtering alone — one missing clause = leakage; RLS fails closed at the database. |
| 0005 | Two-tier cache, COARSE fingerprint (C6) | Per-response exact-match only — misses paraphrases; full semantic-only — non-determinism in billing-grade paths. |
| 0006 | Router selects model **before** final packing (C3 tokenizer truth) | Pack-then-route — wrong tokenizer enforces the hard reserve; risk of 413 or truncation. |
| 0007 | Self-hosted BGE (`bge-small-en-v1.5`, 384-dim) via `EmbeddingProvider` | OpenAI embeddings — per-call cost + egress + a third-party in the hot path (~6 ms p95 in-process vs. network round-trip). |
| 0008 | Single `ReplayResult` schema, deterministic-stages contract (C7) | Ad-hoc per-feature replay payloads — schema drift across API/observability/killer-features. |

Each ADR is linked from the relevant code module via a `# ADR-0006` comment, so a reader navigating the source is one click from the rationale. This closes the "why is it built this way?" loop that kills trust in most repos.

---

## 5. The `.proto` Contracts: Replay Schema as a Versioned Artifact

The `ReplayResult` schema is the product's API surface for its flagship feature, so it must be a **versioned, language-neutral contract**, not a Python dataclass that drifts. We define it in Protocol Buffers under `proto/contextos/replay/v1/`, even though inter-service comms are REST/JSON at the edge today.

**Why protobuf for the schema, REST/JSON at the edge:** the wire format at the gateway stays OpenAI-compatible JSON (drop-in compatibility is non-negotiable for adoption). But the **replay bundle** is a long-lived, content-addressed, cross-language artifact that must survive schema evolution for years of recorded bundles. Protobuf gives us forward/backward compatibility guarantees and a single source of truth that generates Python (control plane), Rust (provisional kernel), and TypeScript (Replay Debugger UI) types. Rejected alternatives: **JSON Schema** — no native codegen across Rust+Python+TS with field-number stability; **Pydantic-only** — Python-locked, and the UI + provisional Rust kernel need the same types; **Avro** — schema-registry ceremony we don't need and worse cross-language ergonomics for nested decision trees.

```proto
syntax = "proto3";
package contextos.replay.v1;

// ONE schema across API, observability, and the Replay Debugger (C7).
message ReplayResult {
  string replay_id = 1;                  // ULID
  string tenant_id = 2;                  // non-null partition key, every object
  string bundle_digest = 3;             // content address (sha256) of the encrypted bundle
  string schema_version = 4;            // "contextos.replay.v1"

  repeated DeterministicStage stages = 5;   // ALL ContextOS decisions, in pipeline order
  BackendInvocation backend = 6;            // the one non-deterministic boundary

  // Asserted ONLY for recorded-output replay. live_backend=true => diff, not equality.
  bool output_equal = 7;
  bool live_backend = 8;
  ReplayDiff diff = 9;                      // populated iff live_backend == true
}

message DeterministicStage {
  // auth_tenant | cache_lookup | retrieve | acl_redact | compress | assemble | route | adapter
  string stage = 1;
  string rfc3339_utc = 2;                 // RFC-3339 UTC timestamp
  uint32 elapsed_ms = 3;
  bytes input_digest = 4;                 // content address of stage input
  bytes output_digest = 5;                // content address of stage output
  google.protobuf.Struct decision = 6;    // stage-specific, fully reconstructable
}
```

The generated artifacts (`*_pb2.py`, Rust prost types, TS) are produced in CI via `buf generate`; **breaking-change detection runs as a CI gate** (`buf breaking --against '.git#branch=main'`). A merge that breaks replay-schema compatibility fails the build. This is what makes "byte-exact replay" a durable promise rather than a demo trick.

---

## 6. CI Gates: The Repository Must Prove Itself on Every Push

Green CI is a trust signal; *what* the CI enforces is the differentiator. We publish the full pipeline in `.github/workflows/ci.yml` and reference it in the README. The headline gate — **the 10,000 hostile-tenant leakage probe** — is the single most screenshot-worthy badge for the security-minded HN/Lobste.rs audience.

| Gate | Tool | Failure condition | Why it's a gate, not advisory |
| --- | --- | --- | --- |
| **Lockfile integrity** | `uv lock --check` | Lockfile out of sync with `pyproject.toml` | Reproducible installs; "works on my machine" is a star-killer. (Rejected: Poetry — slower resolver, weaker hash-locked reproducibility for our distroless images.) |
| **Lint** | `ruff check` + `ruff format --check` | Any lint/format violation | One formatter, fast. (Rejected: black+flake8+isort — three tools, slower, redundant.) |
| **Types** | `mypy --strict` | Any type error | Middleware in a hot path cannot ship `Any`-typed boundaries. Strict, not gradual. |
| **Tests** | `pytest` (+ `pytest-asyncio`) | Any failure; coverage < 85% on core packages | Baseline correctness. |
| **Integration tests** | `docker compose` end-to-end + `pytest` | The compose stack fails to come up, OR a `POST /v1/chat/completions` does not write a `ContextBundle` + `ReplayResult`, OR a recorded-output replay of that `replay_id` is **not bit-exact** (`output_equal == False` or `bundle_digest` mismatch) | Unit tests with mocked dependencies pass even when RLS is misconfigured or the pipeline runs out of order — only a real Postgres+pgvector+Redis+embedder+stub-backend round-trip catches those. (Rejected: mock every dependency — misses RLS/pipeline-ordering regressions; a mock never enforces `SET LOCAL app.tenant_id` or "compress after redact", so the two highest-blast-radius regression classes ship silently green.) |
| **🔒 Tenant-isolation gate** | custom property test (Hypothesis) | **<10,000 hostile second-tenant probes pass, OR any probe reads another tenant's row/vector/cache key** | This is the canonical **0 cross-tenant leakage** guarantee. It encodes the security claim as an executable contract. Non-negotiable, blocking. |
| **Mutation testing on fail-closed paths** | `mutmut` (or `cosmic-ray`) | Surviving mutant in any `@fail_closed`-tagged module (RLS set, RBAC `check`, router hard-policy filter, namespace HARD filter) | A passing test that survives mutation is theater. Fail-closed code (C2/C9/C10) **must** kill 100% of mutants in those modules — otherwise the security tests aren't actually testing the security. |
| **Replay schema compat** | `buf breaking` | Breaking change to `replay/v1` without a version bump | Protects the byte-exact promise across releases (§5). |
| **Container** | `docker build` + `grype` scan | Build fails or HIGH/CRITICAL CVE in distroless image | Production image is the artifact users deploy. |
| **Rust wheels** *(conditional, lands with ADR-0001)* | `maturin build` + `cibuildwheel` | Wheel build/test fails on linux/mac, x86_64/arm64 | When the kernel gate (C14) trips and Rust lands, wheels must be reproducible across platforms or the install path breaks. Until then, this job is a no-op skip. |

**Mutation testing scoped narrowly on purpose.** Running mutation testing on the whole codebase is too slow for PR CI and would be disabled within a week. We scope it to the handful of fail-closed security modules where a surviving mutant is an actual breach risk. This is defensible, fast, and the strongest possible answer to "how do you know RLS is really enforced?" — *because a mutated `SET LOCAL app.tenant_id` makes 10,000 probes fail and a mutated RBAC `check` makes the mutation suite fail.*

### 6.1 Human pentest of the tenant-isolation surface (before M3)

The ≥10,000-probe gate is necessary but **not sufficient**: Hypothesis generates *randomized* second-tenant probes against the data plane, so it reliably catches a dropped `WHERE`/missing `SET LOCAL` (a *property* violation), but it does not reason about *logic and auth-flow* exploits — a probe sequence that escalates RBAC by replaying a stale token, a cache key collision that requires crafting two semantically distinct prompts that hash equal, or an error message that behaves as an oracle. Those are adversarial, multi-step, and intent-driven; random sampling almost never stumbles onto them.

So before the **M3** milestone we commission a **scoped third-party (or dedicated internal) red-team engagement** of the tenant-isolation surface, with a written scope that names the four attack classes the automated gate cannot:

| Attack class | Concrete probe the human runs | What the 10k-probe gate misses here |
| --- | --- | --- |
| **RLS bypass** | Craft a request that reaches a code path where the session GUC is unset or reset (connection-pool reuse, a `SET LOCAL` that didn't fire inside the transaction) and read a foreign row. | Property tests assume the GUC is set per request; they don't fuzz the *pool lifecycle* where it can leak. |
| **Namespace escape** | Coerce the retrieval HARD filter (C2) into resolving a sibling tenant's namespace via path traversal / Unicode-normalization in the namespace string. | Random namespaces are well-formed; an attacker supplies a *malformed* one designed to defeat the filter. |
| **Cache-key / error-oracle leakage** | Send two prompts engineered to collide on the COARSE fingerprint (C6) across tenants; separately, time-or-content-diff error responses to infer whether a foreign key exists. | The gate checks *reads*, not *side-channels*; a timing or 404-vs-403 oracle leaks existence without ever reading a row. |
| **Cross-tenant cache poisoning** | Write to a semantic-cache entry under tenant A and attempt to make tenant B's lookup resolve to it. | Probes read; they don't model a *write-then-foreign-read* sequence. |

**Relationship to the automated gate (stated explicitly, because reviewers will ask):** the 10k-probe gate is the *continuous, every-push regression net* that guarantees a previously-closed hole stays closed; the pentest is the *one-time, human-creativity sweep* that finds the holes the property gate was never shaped to look for. Findings from the pentest are **converted into new deterministic probes and added to the gate**, so each human-found exploit becomes a permanent automated check — the engagement ratchets the gate's coverage upward rather than replacing it. The result is published as a redacted summary in `SECURITY.md`, dated, with the engagement scope. **Rejected alternative: rely on the property gate alone.** Randomized probes provide *breadth* (millions of well-formed accesses) but zero *adversarial depth*; an isolation claim defended only by random sampling is exactly the claim a motivated attacker disproves on day one, and "0 cross-tenant leakage" is too load-bearing a promise to leave to sampling luck.

### 6.2 The end-to-end integration job, step by step

The **Integration tests** row above asserts a full round-trip; here is the exact op-sequence the job runs in CI, because "we have integration tests" is an assertion and a sequence is proof:

```yaml
# .github/workflows/ci.yml (integration job, abridged to the load-bearing steps)
integration:
  services: {}            # not used — we bring the stack up via compose for parity with `docker compose up`
  steps:
    - run: docker compose -f docker-compose.ci.yml up -d   # postgres16+pgvector, redis, embedder(bge-small-en-v1.5), gateway, stub-backend
    - run: uv run contextos migrate && uv run contextos seed --tenant demo
    - run: uv run pytest tests/integration -q                # the assertions below live here
```

```python
# tests/integration/test_end_to_end_replay.py
async def test_chat_writes_bundle_and_replays_bit_exact(gateway, db, redis):
    # 1. real request through the real pipeline (no mocks)
    resp = await gateway.post("/v1/chat/completions", json={
        "model": "contextos-auto",
        "messages": [{"role": "user", "content": "What did we decide about the Q3 launch?"}],
    }, headers={"Authorization": "Bearer demo-tenant-key"})
    replay_id = resp.json()["x_contextos_replay_id"]                       # ULID, surfaced per §2.2

    # 2. assert the side effects were actually persisted (this is what a mock cannot prove)
    bundle = await db.fetchrow(
        "SELECT bundle_digest FROM context_bundles WHERE replay_id = $1", replay_id)
    assert bundle is not None                                              # ContextBundle written
    rr = await db.fetchrow(
        "SELECT schema_version FROM replay_results WHERE replay_id = $1", replay_id)
    assert rr["schema_version"] == "contextos.replay.v1"                   # ReplayResult written

    # 3. recorded-output replay must be bit-exact
    result = await gateway.replay(replay_id, live_backend=False)
    assert result.output_equal is True                                    # byte-for-byte
    assert result.bundle_digest == bundle["bundle_digest"]                # content address stable
```

The job fails if the compose stack does not become healthy, if either row is absent, or if `output_equal` is `False`. Crucially, the pipeline-ordering invariant ("compress ALWAYS after ACL/redact") is observable here: the recorded `DeterministicStage` list in the persisted `ReplayResult` is asserted to be in canonical order, so a reordering regression that a mocked unit test would never surface turns this job red.

---

## 7. License and Governance

- **License: Apache-2.0.** Chosen over MIT for the **explicit patent grant** — infra adopted by companies needs the patent-retaliation clause to be enterprise-legal-approvable; MIT's silence on patents is a procurement blocker. Rejected: **AGPL/SSPL/BSL** — a copyleft or source-available license on developer infra middleware kills exactly the bottom-up adoption (engineers dropping it into an internal stack) that drives stars; we'd trade 1,000 stars for a licensing debate in every thread. Apache-2.0 is the schelling point for serious OSS infra (Kubernetes, Kafka, Cassandra) and signals "you can build on this."
- **`CONTRIBUTING.md`** with a one-command dev setup (`uv sync && uv run pre-commit install`), the ADR rule (§4), and the "every choice names a rejected alternative" norm.
- **`CODE_OF_CONDUCT.md`** (Contributor Covenant) — table stakes; its absence is a smell to the r/MachineLearning crowd.
- **DCO sign-off** (not a CLA). Rejected: CLA — a signing gate suppresses drive-by PRs in the first 1,000-star window, exactly when we need contribution velocity. DCO (`git commit -s`) gives provenance without friction.
- **`SECURITY.md`** with a private disclosure path — mandatory given we make a "0 cross-tenant leakage" claim; the first thing a security researcher does is look for where to report.

---

## 8. Issue and PR Templates

Templates do triage work for us and shape contribution quality. Under `.github/`:

**`ISSUE_TEMPLATE/bug_report.yml`** (GitHub issue forms, not markdown — structured fields are filterable):
- ContextOS version, deployment mode (compose / Helm), backend, embedder.
- **`replay_id`** field, with the line: *"Run `contextos replay <id> --export` and attach the bundle digest."* — this routes every bug straight into our flagship tooling and makes reports reproducible by construction. It is also continuous, organic marketing for the Replay Debugger inside our own issue tracker.
- Pipeline stage dropdown (auth/cache/retrieve/acl/compress/assemble/route/adapter) so issues are pre-labeled by subsystem.

**`ISSUE_TEMPLATE/feature_request.yml`:**
- "Which scope boundary does this respect?" — a required field that links the scope box (§2.4). This politely deflects the steady stream of "make it an agent framework / train models / be a vector DB" requests by forcing the requester to confront the boundary. Saves maintainer energy and keeps the project from drifting.

**`PULL_REQUEST_TEMPLATE.md`** checklist:
- [ ] ADR added/updated if this changes an architecture decision (names a rejected alternative).
- [ ] `uv lock` updated if dependencies changed.
- [ ] Touches a fail-closed path? Mutation suite passes locally.
- [ ] Changes `replay/v1`? Version bumped; `buf breaking` considered.
- [ ] New canonical number? Matches the latency/cost facts in §9/Section 9.

**`config.yml`** disabling blank issues and routing questions to GitHub Discussions, keeping the issue tracker as a clean, high-signal surface that new visitors judge the project by.

---

## 9. Launch Surfaces and the Launch-Week Sequence

A launch is a *sequence*, not an event. Firing everything on one day wastes the asset and exhausts the maintainers during the exact 48 hours when comment-response latency determines whether a thread lives or dies. We stage it, front-loading the highest-trust, most-technical audiences who will scrutinize (and, if won, validate) the project for everyone downstream.

### 9.1 Pre-launch (T-7 to T-1): make the repo unimpeachable before any link is posted

- README hero GIF final, quickstart verified on a clean machine (the single highest-leverage QA task — a broken quickstart on launch day is fatal).
- All day-zero ADRs (§4) merged. `proto/replay/v1` published. All CI gates green, including the 10k-probe gate, with the badge live.
- **Demo repo** `contextos-demo` published (see §10) and its 5-minute path re-verified.
- **Launch blog post** finalized and staged (see §10): *"Your RAG can't tell you why — ContextOS can."*
- Seed Discussions with 3–4 genuine FAQ threads ("How is this different from LiteLLM/mem0?", "Does the quickstart need a GPU?") so the first visitors don't land on an empty community tab.

### 9.2 Launch week sequence

| Day | Surface | Asset / framing | Why this slot |
| --- | --- | --- | --- |
| **Mon** | **r/LocalLLaMA** | Self-hosted angle: "ContextOS — context middleware with byte-exact replay, runs fully local (self-hosted BGE, no API keys)." Lead with the GIF. | Warmest, most self-host-aligned audience; their early upvotes + critique harden the repo before the harsher surfaces. Local-first is exactly their value. |
| **Tue** | **Hacker News — Show HN** | "Show HN: ContextOS — see exactly why your LLM said that (byte-exact context replay)." Link to repo, not blog. Maintainer present all day to answer. | The single biggest star spike if it lands. Tuesday morning ET is the high-traffic, high-quality window. Requires Monday's feedback already absorbed so the front-page version is bulletproof. |
| **Tue** | **Lobste.rs** (`distributed`, `databases`, `ai` tags) | Same Show-HN framing; this audience reads the ADRs and the RLS/mutation-testing gates. | Smaller but maximally technical; a positive Lobste.rs thread is durable social proof. Posted same day to ride the HN energy with an audience that scrutinizes internals. |
| **Wed** | **Launch blog post** → **X/Twitter** thread | Publish *"Your RAG can't tell you why — ContextOS can"*; a 6–8 tweet thread, each tweet one frame of the replay story, ending on the quickstart. Tag relevant infra/LLM accounts. | Mid-week, after HN has produced quotes/stars to cite as proof ("#1 on HN today"). The thread is the shareable artifact that carries beyond launch week. |
| **Wed** | **vLLM Discord + LangChain Discord** | Composition framing, not competition: "We export OTel to Langfuse, can sit in front of vLLM, and adapt to LiteLLM. Here's the replay debugger." | These communities reward "works with my stack." Leading with composability (§3 footer) turns potential rivals' communities into adopters. |
| **Thu** | **r/MachineLearning** (`[P]` Project) | Rigor framing: the latency budget table (Section 9), the 0-leakage CI gate, the compression fact-retention (≥98%, NLI-guarded). Less hype, more numbers. | This audience punishes marketing and rewards quantified claims. Posting later lets us cite HN/Lobste.rs validation and arrive with battle-tested answers. |
| **Fri** | **Recap + first "good first issues"** | A short Discussions post: launch-week stats, top questions answered, 8–10 labeled `good-first-issue` tasks. | Converts launch attention into contribution. The window between "starred it" and "forgot it" is days — give them a way in immediately. |

### 9.3 The numbers we lead with on every surface (from the canonical facts — never improvised)

Each post anchors on a *subset* of these, matched to the audience. Consistency across surfaces is what makes the project look engineered rather than hyped:

- **Context assembly < 50 ms p95** (score+MMR+knapsack over ≤512 candidates).
- **Total ContextOS control overhead < 250 ms p95** (excludes model inference; it's the critical-path p95, not a naive sum of stages).
- **pgvector HNSW ANN probe p95 = 18 ms** at ≤5M vectors/tenant (Qdrant cutover holds ≤25 ms beyond).
- **Exact-hash cache < 1 ms p99** (Redis); semantic-ANN cache **8–15 ms p95**.
- **0 cross-tenant leakage**, CI-gated by **≥10,000 hostile probes**.
- **40–65% token-cost savings** (caching 15–30% + routing downgrade 20–40% of spend on easy queries + compression 10–25% prompt-token reduction).
- **Cache hit-ratio 25–45%** on a realistic mixed workload (COARSE fingerprint).
- **Compression 2–4× with ≥98% fact retention** (NLI-guarded).
- **Gateway 5k–10k req/s per node**; **99.9% availability** (≥3 replicas across ≥3 AZ, PDB minAvailable=2).

**Discipline rule:** every contributor and every social post uses these exact figures. Inventing a different number (e.g., "15 ms ANN") in a thread, once spotted by a skeptic, undermines every other claim. The canonical-facts table is the law for external comms, not just the code.

### 9.4 Surfaces we deliberately skip in week one

- **Product Hunt** — wrong audience for self-hosted infra middleware; rewards consumer polish over engineering rigor. Rejected for launch; reconsider only if a hosted offering appears.
- **Paid ads / influencer pushes** — buys hollow stars that don't convert to issues/PRs and reads as inauthentic to the exact technical crowd we need. Rejected.

---

## 10. The Demo Repo and the Launch Blog

### 10.1 `contextos-demo` (separate repo, linked from README and blog)

A separate repository, not a `examples/` folder, because it can be starred independently, cloned without the full source, and is the artifact every blog/social link points at. It contains:

- A realistic **RAG-over-internal-docs** scenario (a fictional company's Slack/Confluence-style corpus) where naive RAG visibly gives a *wrong-looking* answer.
- A single command — `make replay` — that opens the Replay Debugger on that exact query and shows **why**: the relevant chunk was *dropped by the budget knapsack* (visible in the assembly stage decision), or *redacted by ACL* before compression. The user sees the causal chain, not just the output.
- The "before/after" that proves the wedge: *here is the answer; here is the byte-exact reason it came out that way.*

The demo's job is to make the abstract promise ("see why your LLM said that") concrete and reproducible in 2 minutes. It is the GIF, made runnable.

### 10.2 Launch blog: *"Your RAG can't tell you why — ContextOS can"*

Structure (engineering essay, not a press release):

1. **The cold open — a debugging horror story.** A RAG answer that's confidently wrong. You stare at logs. You can see the final prompt (maybe), but not *why* it was assembled that way — which memory won, which got compressed, which the budget dropped. This is the universal, visceral pain. Hook the reader with their own Tuesday afternoon.
2. **Why this is structurally unsolvable in today's stacks.** mem0 tells you what's in memory, not why a given item made it into *this* prompt. Langfuse traces the call, not the *deterministic context decisions* upstream of it. GPTCache/LiteLLM operate at the wrong layer entirely. Name them, fairly. The gap is real and nobody owns it.
3. **The insight: context assembly is a deterministic pipeline, so it's replayable.** Walk the **pipeline invariant** (auth → cache → retrieve → ACL/redact → compress → assemble → route → adapter → stream → write-back) and the key property: **every stage except `backend.invoke` is deterministic**, so given a content-addressed, per-tenant-encrypted bundle, ContextOS reconstructs each decision **byte-for-byte** (the C7 contract). Show the `ReplayResult` schema (§5). Explain `output_equal` (recorded replay) vs. the live-backend *diff*.
4. **The demo, inline.** Embed the GIF and the `contextos-demo` `make replay` flow. This is where the reader goes from "interesting" to "I need this."
5. **It's also fast and safe — the numbers.** A tight table from §9.3: < 250 ms p95 overhead, < 50 ms assembly, 18 ms ANN, 0 cross-tenant leakage (10k-probe CI gate), 40–65% cost savings. Establish that replay isn't a research toy bolted onto a slow prototype — it's production middleware.
6. **The honest scope and the call to action.** What ContextOS is *not* (the scope box), how it composes with your existing stack (the §3 footer), and the 5-minute quickstart. End on the repo link and "star us / open a `good-first-issue`."

The blog post is the durable asset; the social/forum posts are pointers to it (except Show HN, which points at the repo). Its title is also a tweet, a subreddit headline, and a one-line pitch — chosen so the positioning is identical on every surface.

---

## 11. Success Metric and the 1,000-Star Mechanism, Explicitly

We are not hoping for stars; we are engineering a funnel:

```
Demo GIF (8s)  ->  "is it real?" -> 5-min quickstart succeeds  ->  replay_id surfaced
   -> user runs `contextos replay` -> sees WHY (the wedge lands) -> star
   -> good-first-issue / Discussions  ->  contributor
```

Every section above maps to one funnel stage: the GIF and comparison table win the 8 seconds; the hermetic offline quickstart and green CI gates answer "is it real?"; the surfaced `replay_id` forces the wedge moment; the ADRs/templates/Apache-2.0 convert the impressed visitor into a contributor. The 1,000 stars are the *output* of that machine running across the launch-week sequence, not a vanity target chased directly.

---

### Cross-section dependency note

This section assumes the response object exposes a **`x_contextos_replay_id` (ULID)** on every `/v1` response and that `contextos replay <id>` and `contextos replay <id> --export` are first-class CLI commands — the **API section and CLI/observability sections must surface this exact field name and these commands** for the quickstart, issue templates, and demo to be consistent. It also assumes the `ReplayResult` protobuf lives at `proto/contextos/replay/v1/` with `schema_version = "contextos.replay.v1"` and is the **single** replay schema (C7); the API and observability sections must reference that same package path and version string rather than defining a parallel JSON schema. All external-facing numbers used here are quoted verbatim from the canonical-facts table and the Section 9 latency budget — no figure is introduced or altered.
