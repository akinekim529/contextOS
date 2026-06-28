# Killer Features: The Replay Substrate and What It Makes Possible

ContextOS earns its keep by removing a category of pain that no application team has been able to solve from inside their own stack: **"the model gave a different / worse / wrong answer last Tuesday and nobody can tell me exactly what context it saw."** Every retrieval-augmented system today is non-reproducible by construction. The retrieval index drifted, the candidate scores were thrown away, the prompt was assembled by a function whose inputs were ephemeral, and the only artifact that survived is the final completion text. Debugging a context-assembly regression is therefore archaeology, not engineering.

This section designs the feature that fixes that — the **Context Replay Debugger** — and then five supporting features that are not independent products but *consequences of the same substrate*. The thesis of this entire section:

> **Replay is the wedge. The bundle is the substrate. Everything else is a view over the bundle.**

If you can freeze, content-address, encrypt, and byte-exactly replay every context decision, then cost attribution, cache sharing, memory versioning, context diffing, and agent-trace correlation are all just *queries and joins over frozen bundles*. We build the hard thing once and amortize it across the feature surface. The alternative — bolting a bespoke logging path onto each feature — was rejected because it produces five inconsistent, partially-sampled, mutually-incoherent trails that never agree on what happened, which is exactly the failure mode we are selling against.

---

## 1. FLAGSHIP — The Context Replay Debugger

> **FLAGSHIP FEATURE.** This is the product wedge. If a buyer remembers one thing about ContextOS, it is "byte-exact replay of every context decision."

### 1.1 What it is and why it is the wedge

The Context Replay Debugger takes any historical request — identified by its `request_id` (ULID) — and reconstructs, **byte-for-byte**, every deterministic decision ContextOS made: which candidates were retrieved, their raw per-modality scores, the final ranking, the MMR diversity selection, the knapsack packing decision under the token budget, the redaction/ACL masks applied, the compression performed, the routing decision, and the **exact rendered prompt string** that was handed to the backend adapter.

The wedge works because replay is *tangible*. A platform team does not buy "observability" — they have ten dashboards already. They buy the ability to take a production incident ("this answer leaked a stale fact / cited the wrong document / blew the token budget and got truncated") and answer, in under a minute, **"here is the precise context the model saw and here is the single decision that produced the defect."** That is a debugging primitive, not a vanity metric, and it is the thing that turns a black-box pipeline into an engineered system.

**Rejected framing.** We considered shipping replay as "trace search over OpenTelemetry spans" (i.e., reconstruct context from spans). Rejected: spans are sampled (C12: tail 1–10%), lossy, and string-typed; you cannot reconstruct a 512-candidate scoring matrix or a per-tenant-encrypted prompt from a span's attribute map without exceeding span size limits and leaking PII into the trace store. Replay needs a *first-class, complete, encrypted artifact*, not a side-effect of tracing.

### 1.2 The decision-replay vs generation-replay boundary (C7)

This is the single most important contract in the feature, and it is the line every dishonest "replay" product blurs. **ContextOS replays decisions deterministically; it does not pretend the model is deterministic.**

| Stage class | Examples | Replay guarantee |
| --- | --- | --- |
| **Deterministic stages** | tenant/auth resolution, cache fingerprint, candidate set, raw scores, final ranking, MMR seed + selection, knapsack packing, ACL/redaction masks, compression output, router decision inputs, **rendered prompt bytes** | **Byte-exact.** Re-running the recorded inputs through the same pinned config hash reproduces identical output. Asserted in CI and at replay time. |
| **Non-deterministic stage** | `backend.invoke` (the actual LLM forward pass) | **Not reproducible.** Sampling temperature, backend model weights, and provider-side nondeterminism make this irreproducible *by definition*. We never claim otherwise. |

There are exactly three replay modes, and the schema below names which guarantee each yields:

1. **`recorded_output` replay (default, offline).** We replay deterministic stages and compare against the *frozen* recorded prompt + recorded completion. `output_equal` is asserted **only here**, because both sides are recorded artifacts. This is the mode used in CI regression gates and post-incident forensics.
2. **`live_backend=True` replay.** We replay deterministic stages, rebuild the exact prompt, then **re-invoke a live backend**. Because `backend.invoke` is non-deterministic, this yields a **diff**, never byte-equality. This answers "if I re-ask today with the same context, does the answer drift?" — a model-regression question, not a context-regression question.
3. **`config_override` replay (counterfactual).** We replay the recorded candidates + scores but swap a config knob (e.g., a new MMR lambda, a different compression ratio, a changed token budget). This produces a deterministic *new* assembly decision plus a diff against the original. This is how you A/B a pipeline change against real historical traffic without touching production.

```python
# C7: the ONE ReplayResult schema, shared verbatim across the public API
# (Section 5 / API), observability (Section 8), and this killer-feature section.
# No section may redefine these fields.

class ReplayMode(StrEnum):
    RECORDED_OUTPUT = "recorded_output"   # offline, output_equal assertable
    LIVE_BACKEND    = "live_backend"      # re-invokes model -> diff only
    CONFIG_OVERRIDE = "config_override"   # counterfactual deterministic re-assembly

class StageReplay(BaseModel):
    stage: str                      # "retrieve" | "acl" | "compress" | "assemble" | "route" | ...
    deterministic: bool             # True for all ContextOS stages; False for backend.invoke
    input_hash: str                 # blake3 of canonical-JSON inputs
    output_hash_recorded: str       # blake3 of what was recorded at request time
    output_hash_replayed: str | None  # blake3 of what replay produced (None if not run)
    equal: bool | None              # output_hash_recorded == output_hash_replayed; None if non-deterministic

class ReplayResult(BaseModel):
    request_id: ULID
    tenant_id: ULID                 # non-null partition key, always present
    bundle_cid: str                 # content address of the source bundle (blake3)
    mode: ReplayMode
    config_hash_recorded: str       # pinned config the request actually ran under
    config_hash_replayed: str       # pinned config replay ran under (differs iff CONFIG_OVERRIDE)
    stages: list[StageReplay]
    prompt_equal: bool              # rendered-prompt bytes identical (deterministic stages)
    # output_equal is asserted ONLY in RECORDED_OUTPUT mode; it is None otherwise.
    output_equal: bool | None
    diff: ContextDiff | None        # populated in LIVE_BACKEND and CONFIG_OVERRIDE
    replayed_at: str                # RFC-3339 UTC
```

**Invariant, stated bluntly:** `output_equal` is `None` unless `mode == RECORDED_OUTPUT`. Any caller that reads `output_equal` in `LIVE_BACKEND` mode is misusing the API; the field is structurally `None` to make that misuse impossible rather than merely discouraged.

### 1.3 The context bundle: content-addressed, per-tenant-encrypted

The bundle is the frozen, self-describing record of one request's context journey. It is **content-addressed** (its identity is `blake3` of its canonical bytes) and **per-tenant-encrypted** (sealed under the tenant's data-encryption key, DEK).

```jsonc
// ContextBundle v1 — canonical CBOR on the wire, shown here as JSON for readability.
{
  "schema": "contextos.bundle.v1",
  "bundle_cid": "b3:9f2c...e41a",          // blake3 of the canonical-CBOR body below
  "tenant_id": "01J8...TENANT",            // partition key; never null
  "namespace": "proj_alpha/agent_7/user_42", // C2 within-tenant hard scope
  "request_id": "01J8...REQ",
  "created_at": "2026-06-28T14:03:22.118Z",  // RFC-3339 UTC
  "config_hash": "b3:4d10...77",           // pins EVERY knob: weights, MMR lambda,
                                           // budget, compression model+ratio, tokenizer id,
                                           // router policy version, embedder version
  "seeds": { "mmr": 7741, "tiebreak": 1188 }, // determinism anchors (see 1.4)
  "candidates": [                          // <= 512 (scope-boundary hard cap)
    {
      "doc_id": "01J8...DOC",
      "modality": "semantic",              // semantic | episodic | bm25 | working
      "raw_scores": {                      // C1: RAW per-modality scores only.
        "cosine": 0.8123,                  // Memory Engine NEVER final-ranks.
        "bm25": 11.4,
        "recency_decay": 0.62              // orthogonal recency signal
      },
      "ciphertext_ref": "b3:aa01...",      // payload sealed under tenant DEK
      "token_len": 142
    }
    // ... up to 512
  ],
  "final_ranking": [ /* ordered doc_id list AFTER assembler scoring + MMR */ ],
  "acl_decisions": [ { "doc_id": "...", "action": "allow|redact|drop", "rule": "rbac:..." } ],
  "compression": { "model": "bge-compress-v1", "ratio": 3.1, "nli_fact_retention": 0.991 },
  "rendered_prompt_ref": "b3:cc77...",     // the EXACT bytes sent to the adapter, sealed
  "route": { "selected_model": "haiku-tier", "policy_version": "rp_v14",
             "reason": "difficulty=0.21<downgrade_threshold" },
  "recorded_output_ref": "b3:dd88...",     // sealed completion (null until backend terminal)
  "cost_ledger_ref": "01J8...COST"         // FK into billing-grade outbox (C12)
}
```

**Why content-addressing.** The `bundle_cid` *is* the integrity proof. If anyone mutates a candidate score after the fact, the CID changes, and the replay's `config_hash`/`bundle_cid` chain breaks loudly. This gives us tamper-evidence for free and lets the supporting features (cache sharing, memory versioning, diffing) reference immutable bundles by hash without copying bytes. **Rejected:** monotonic integer bundle IDs in Postgres. They give no integrity guarantee, force a central allocator on the hot path, and cannot be deduplicated across identical contexts (two requests with byte-identical context share one CID; an integer ID cannot).

**Why per-tenant encryption.** Candidate payloads and rendered prompts contain the tenant's private data and grounded facts. Storing them plaintext in a replay store is a cross-tenant catastrophe waiting to happen and makes RTBF (C11) impossible to honor. Sealing every payload/prompt/output under the **per-subject DEK** means crypto-shred deletes are real: drop the DEK and the bundle is permanently unreadable, embeddings included (C11). **Rejected:** column-level Postgres encryption only. It protects the relational rows but leaves the large prompt/candidate blobs (which live in object storage) unprotected and does not give us subject-scoped key destruction.

### 1.4 Determinism anchors

Byte-exact replay requires that every "random" decision was actually pseudo-random under a *recorded* seed:

- **MMR selection** uses `seeds.mmr` to break exact-tie diversity choices. Same seed + same candidate scores ⇒ same selection order.
- **Stable tie-breaks** in ranking use `seeds.tiebreak` plus the candidate `doc_id` (ULID, lexicographically sortable) as the final, fully-deterministic tie key. There is no `dict` iteration order or `set` nondeterminism anywhere in the assembly path; all collections are explicitly sorted by `(score_desc, doc_id_asc)`.
- **Tokenizer identity** is pinned in `config_hash`. Per C3, the router selects the model *before* final packing so the correct tokenizer enforces the hard reserve; the tokenizer id is part of the config hash precisely so replay packs against the same tokenizer it originally used. If the original packing fell back to the conservative max-tokenization estimate (router couldn't pin a model in time), that fallback *and its documented margin* are recorded, and replay reproduces the same fallback rather than silently using the now-known tokenizer.

### 1.5 Two-phase write: never put bundle persistence on the hot path

A full bundle is large (up to 512 candidates plus a rendered prompt plus a completion). Persisting and encrypting it synchronously would obliterate the latency budget. So we split the write.

**Phase 1 — synchronous pointer stub (on the hot path).** Before the adapter dispatch, we write a tiny, fixed-size **pointer stub** and enqueue the heavy work. The stub is the durable promise that a bundle exists; it costs us the `2 ms p95` "async write-back enqueue" line in the Section 9 latency table — and *only* that line.

```python
async def freeze_phase1(ctx: AssembledContext) -> BundleStub:
    # Runs INSIDE the request, AFTER assembly/route, BEFORE/at adapter dispatch.
    # Pure CPU: compute the content address over already-materialized decisions.
    cid = blake3_canonical(ctx.deterministic_view())     # candidates+scores+seeds+config_hash+prompt
    stub = BundleStub(
        bundle_cid=cid, request_id=ctx.request_id, tenant_id=ctx.tenant_id,
        namespace=ctx.namespace, status="pending", created_at=now_rfc3339(),
    )
    # Redis Streams is the async plane AND the replay log (one substrate, C: locked arch).
    await redis.xadd(f"replay:write:{ctx.tenant_id}", {"stub": stub.to_cbor()})
    return stub  # <= 2 ms p95; the only replay cost charged to the hot path
```

**Phase 2 — asynchronous full-bundle persistence (off the hot path).** A custom asyncio consumer drains `replay:write:{tenant_id}`, seals each payload under the tenant DEK, writes the bundle body to content-addressed object storage keyed by `bundle_cid`, and flips the stub `status` from `pending` to `committed` in Postgres (tenant-partitioned, FORCE RLS). The completion (`recorded_output_ref`) is attached here as well, gated by the client-abort rule below.

```python
async def freeze_phase2(stub: BundleStub):
    body = await assemble_full_bundle(stub)           # gather candidates, prompt, output
    sealed = seal_with_tenant_dek(body, stub.tenant_id)  # per-tenant encryption (C11 scope)
    await object_store.put(stub.bundle_cid, sealed)   # content-addressed; idempotent on CID
    await pg.execute(                                  # RLS-scoped, tenant_id partition
        "UPDATE replay_bundles SET status='committed', body_ref=$1 "
        "WHERE bundle_cid=$2 AND tenant_id=$3",
        sealed.ref, stub.bundle_cid, stub.tenant_id,
    )
```

Because writes are keyed by `bundle_cid`, Phase 2 is **idempotent and deduplicating**: replaying the same stream entry, or two requests producing byte-identical context, results in exactly one stored object. **Rejected alternative:** a synchronous write-ahead to Postgres of the full bundle. It adds tens of milliseconds of serialize+encrypt+fsync to every request, violating the `< 250 ms` control-overhead budget for zero hot-path benefit, since nobody reads the bundle on the request's critical path.

### 1.6 Client-abort and the recorded output (C8)

The completion is only durably attached to the bundle if the **server reached a terminal event**:

- **Server `finish_reason` reached ⇒ commit.** The terminal-event source is the backend's own finish signal. We attach `recorded_output_ref` and let cost write-back proceed. This holds even if the client's TCP connection died mid-stream *after* the server finished — the work was done, the cost was incurred, the bundle is complete.
- **Client TCP close *before* server terminal ⇒ discard the output.** The stub remains (`status` reflects `aborted`), the deterministic stages are still fully replayable (they completed before dispatch), but `recorded_output_ref` is null and we record **partial-cost attribution**: the prompt tokens already committed to the backend plus any streamed completion tokens the provider will bill for, charged to the tenant's budget ledger as `partial_abort`. This is cited identically in the API and adapter sections; ContextOS does not invent a second abort semantics.

This means a replay of an aborted request honestly reports: deterministic stages = byte-exact, `output_equal = None` (no recorded output exists to compare), and the cost ledger shows the partial charge. No phantom completions, no double-billing.

### 1.7 Replay UX sketch

```
$ contextos replay 01J8XQ...REQ --mode recorded_output

  bundle b3:9f2c…e41a   tenant proj_alpha   ns proj_alpha/agent_7/user_42
  config b3:4d10…77     recorded 2026-06-28T14:03:22Z

  STAGE          DETERMINISTIC  EQUAL   NOTES
  ─────────────────────────────────────────────────────────────
  cache          yes            ✓       miss (semantic, 11ms)
  retrieve       yes            ✓       512 candidates, RRF fused
  acl/redact     yes            ✓       3 redacted, 1 dropped (rbac:pii_block)
  compress       yes            ✓       3.1× , NLI fact-retention 0.991
  assemble       yes            ✓       MMR λ=0.7 seed=7741, knapsack fit 7,944/8,192 tok
  route          yes            ✓       haiku-tier (difficulty 0.21 < 0.30 downgrade)
  backend.invoke NO             —       non-deterministic (output recorded)

  prompt_equal = TRUE      output_equal = TRUE      ✅ byte-exact replay
```

The web UI renders the same `ReplayResult` as a vertical pipeline; clicking the `assemble` stage opens the 512-row scoring matrix (raw per-modality scores ↔ final rank), and clicking `route` opens the routing decision (covered in §2). Every panel is a view over the *same bundle*.

---

## 2. Supporting feature — Cost-aware routing auto-downgrade (live $/quality dashboard)

**User value.** Teams overpay by sending trivial queries ("summarize this 3-line note", "is this sentiment positive?") to frontier-tier models. Auto-downgrade routes easy queries to a cheaper tier and *shows the user the money it saved and the quality it preserved*, in real time. This is where the canonical **20–40% model-routing downgrade** slice of the **40–65% total token-cost savings** becomes visible and defensible.

**UX sketch.**

```
 ┌─ Routing & Spend (last 1h, tenant proj_alpha) ───────────────────────┐
 │  Requests 18,402    Downgraded 41%    Spend $  214.10  ▼ saved $131  │
 │                                                                       │
 │  difficulty  ░░░░▓▓▓▓████   downgrade threshold 0.30                  │
 │  tier mix    frontier 31%  │  mid 28%  │  haiku-tier 41%             │
 │  quality Δ   −0.4%  (NLI agreement vs frontier on 2% shadow sample)  │
 │                                                                       │
 │  ⚠ residency: EU traffic pinned to eu pool (hard filter, never down- │
 │     graded across region)                                            │
 └───────────────────────────────────────────────────────────────────────┘
```

**Technical mechanism.** The router scores each request's *difficulty* (cheap in-process features: token count, retrieval-score dispersion, query embedding norm, presence of code/math markers) and computes a *utility* = expected quality per dollar. If difficulty is below the downgrade threshold, it selects the cheaper tier. Crucially, this obeys the locked router posture:

- **Hard-policy filters fail CLOSED on static policy (C9).** Allowlist, residency, capability, and budget are evaluated against the *static* policy and do **not** depend on the health store. The safe-default pool itself satisfies every hard filter — **residency is never bypassed by a downgrade**, even if the health/latency signals are unavailable. Only the optimization signals (latency, queue depth, quality estimate) fail *open* to a static ranking.
- **RBAC is the single routing authority (C10).** The router calls `check(principal, resource=model, action='route')`; `RoutePolicy.allowed_backends` derives from that one call. There is no second allowlist store to drift out of sync.
- **GPU-aware routing is a telemetry READER, never a scheduler (scope invariant).** The dashboard's queue-depth bars read GPU/queue telemetry; ContextOS never places, schedules, or preempts GPU work.

Every routing decision (selected tier, difficulty, threshold, reason, residency pin) is written into the bundle's `route` field. The dashboard is therefore not a separate metrics pipeline — it is a **time-bucketed aggregation over bundle `route` fields joined to the cost ledger**, which is why the dollar figures reconcile exactly with billing (C12 durable cost outbox) rather than approximately.

---

## 3. Supporting feature — Secure cross-user semantic cache sharing

> **OFF BY DEFAULT. Gated behind a mandatory security review before any tenant may enable it.** This feature trades isolation for hit-rate and must never be on implicitly.

**User value.** Within an organization, two users often ask semantically equivalent, non-private questions ("what's our refund policy?"). Sharing a semantic-cache entry across users lifts the cache hit-ratio toward the upper end of the canonical **25–45%** band and shaves the **15–30% caching** slice of cost savings — *but only for content that is provably safe to share*.

**UX sketch.**

```
 Org cache sharing:  [ OFF ]  ← default. Toggle requires security-review sign-off.

 When ON (shared-org namespace, C2 opt-in):
   "what is our refund policy?"  →  served from shared entry (authored by user_19)
        share predicate: ✓ non-private  ✓ no memory-grounded private facts
                         ✓ same system_prompt_version  ✓ RBAC cache_read granted
   "what did *I* email Acme last week?"  →  NEVER shared (memory-private-grounded)
```

**Technical mechanism — the content-authorization predicate.** An entry is shareable across users **iff every clause holds**:

```python
def shareable(entry: CacheEntry, requester: Principal) -> bool:
    return (
        entry.namespace_scope == "shared_org"                       # C2: opt-in shared namespace
        and rbac.check(requester, resource="cache", action="cache_read")  # C10 action enum
        and not entry.memory_private_grounded                       # C6: private-grounded = non-cacheable/-shareable
        and entry.fingerprint.system_prompt_version == requester.system_prompt_version
        and entry.fingerprint.stable_fact_set_version == requester.stable_fact_set_version
    )
```

The cache key is the **COARSE fingerprint (C6)**: `hash(normalized-query-embedding-bucket + model_id + system_prompt_version + stable_fact_set_version)`. Sharing operates only on the **semantic-ANN tier** (8–15 ms p95, pgvector/Qdrant); the **exact-hash tier (< 1 ms p99, Redis)** stays per-user-namespaced because sub-millisecond is its whole point and cross-user normalization would defeat it. Any response that was grounded in a user's private memory is flagged `memory_private_grounded` at assembly time and is structurally **non-cacheable** (C6), so it can never enter a shareable entry regardless of the toggle. The shared entry references the original author's **bundle `bundle_cid`**, so a reviewer auditing "why did user_42 get user_19's cached answer?" replays the *exact* originating context — the security review is itself a replay query.

---

## 4. Supporting feature — Git-like memory versioning (branch / diff / rollback)

**User value.** Memory is mutable and consolidation (the async, rate-limited, cost-tracked batch job) rewrites it over time. Teams need to *experiment* with memory ("what if we forget everything before Q1?", "branch the agent's memory, try a new consolidation policy, compare") and *recover* from a bad consolidation ("roll back last night's batch — it over-merged two customers"). Git-like semantics make memory a versioned asset, not a fragile mutable blob.

**UX sketch.**

```
$ contextos memory branch experiment/aggressive-decay --from main@2026-06-27
$ contextos memory diff main experiment/aggressive-decay
   ~ 1,204 memories re-scored (recency decay λ 0.01 → 0.03)
   - 318 memories dropped below retention floor
   + 0 added
$ contextos memory rollback main --to 2026-06-26T00:00:00Z   # undo last consolidation
   restored 318 memories;  new HEAD main@b3:71aa…
```

**Technical mechanism.** Memory versioning rides the **same content-addressed substrate** as bundles. Each consolidation batch produces an immutable **memory snapshot** — a Merkle DAG whose leaves are content-addressed memory records (sealed under the tenant DEK) and whose root is a `snapshot_cid`. A "branch" is a named pointer to a `snapshot_cid`; a "commit" is a new root that structurally shares unchanged leaves with its parent (so a branch costs only the deltas, not a full copy). `diff` walks the two DAGs and reports added/dropped/re-scored records. `rollback` is just repointing `HEAD` to an earlier root — non-destructive, because old roots remain content-addressed and reachable until GC.

This obeys the scope invariants precisely:

- **Consolidation is an async, rate-limited, COST-TRACKED batch job, not an agent loop (scope invariant).** Branching/diffing never triggers inference except through that batch job, and **the job's inference cost enters the budget ledger** — a memory experiment that re-embeds 1,204 records shows up as a line item, not free compute.
- **RTBF interlocks with versioning (C11).** A crypto-shred for a subject drops that subject's DEK; the corresponding leaves become permanently unreadable **across all snapshots and branches simultaneously**, and the idempotent GC sweep tombstones the now-dangling references. You cannot resurrect a deleted subject by checking out an old branch — the bytes are cryptographically gone, embeddings included.
- **Recency-decay vs assembler ordering stay orthogonal (C1).** Versioning operates on the memory-decay/recency dimension (what is retained, at what decayed score). It does **not** encode the assembler's lost-in-the-middle edge-placement ordering, which is a per-request assembly decision recorded in the bundle, not a property of the memory snapshot. One weight vocabulary, two orthogonal axes.

---

## 5. Supporting feature — Context diff tool

**User value.** "Why did request A and request B behave differently?" is answered by *diffing their contexts*. The diff tool compares two bundles (or a bundle against a counterfactual `config_override` replay) and visualizes exactly what changed in the assembled context — which candidates entered/left, how scores moved, what the redaction layer masked differently, and how the rendered prompt diverged token-region by token-region.

**UX sketch.**

```
 contextos diff  A=01J8XQ…  B=01J8YR…

  CANDIDATES        A→B
    + doc 01J…D9   entered  (cosine 0.79, ranked #4)
    − doc 01J…C1   left     (was #6; dropped by ACL: rbac:pii_block)
    ~ doc 01J…A0   rank 2 → 5   (recency_decay 0.71 → 0.48)

  PROMPT (rendered, deterministic)
    system     ═  identical (system_prompt_version v14)
    facts      ~  3 lines changed  ──────────────────────────
        - "Plan: Enterprise (renewed 2025-11)"
        + "Plan: Enterprise (renewed 2026-05)"        ← stale_fact_set_version bumped
    history    ═  identical
    budget     7,944 → 8,012 tok   (knapsack admitted +1 candidate)

  ROUTE          haiku-tier → mid-tier   (difficulty 0.28 → 0.34 crossed 0.30)
```

**Technical mechanism.** The diff engine operates purely over the **deterministic** portions of two bundles, because only deterministic stages are comparable (C7). It produces the `ContextDiff` object referenced by `ReplayResult.diff`, so the *same* structure powers replay's `live_backend`/`config_override` outputs and the standalone diff tool — one diff type, three entry points. Diffing decrypts both bundles under the requesting tenant's DEK inside the tenant's RLS scope; **cross-tenant diffs are impossible by construction** (the DEKs are different and RLS forbids the join), which preserves the zero-cross-tenant-leakage invariant. The rendered-prompt diff is computed over token regions (system / facts / history / tools) rather than raw characters, so a reviewer sees *semantic* drift ("a stale fact changed") rather than noise. Because it is a pure function of two immutable, content-addressed bundles, the diff itself is cacheable and deterministic — diffing the same `(cid_a, cid_b)` pair always yields the same result.

---

## 6. Supporting feature — Agent-execution tracing (READ-ONLY span correlation)

> **READ-ONLY. ContextOS correlates agent-trace spans; it NEVER schedules, retries, or re-executes a step (C13 / scope invariant).** ContextOS is not an agent framework and must not drift into one.

**User value.** Multi-step agents (plan → tool → tool → synthesize) make many LLM calls, each with its own assembled context. When an agent run goes wrong, the operator needs to see the *whole chain*: which step produced which context, what each step retrieved, where cost concentrated, and which single step's context caused the cascade. Agent-execution tracing stitches the per-step bundles into one navigable run.

**UX sketch.**

```
 run 01J8…RUN   agent_7   4 steps   $0.061   2.9s wall
 ├─ step 1  plan        haiku-tier   ctx b3:11…  $0.004   "decompose task"
 ├─ step 2  tool:search mid-tier     ctx b3:22…  $0.019   12 candidates → 3 used
 ├─ step 3  tool:fetch  haiku-tier   ctx b3:33…  $0.006   ⚠ stale fact admitted here
 └─ step 4  synthesize  frontier     ctx b3:44…  $0.032   inherited stale fact → wrong answer
                                                  ↑ click any ctx to REPLAY that step
```

**Technical mechanism.** Each agent step that flows through the ContextOS gateway already emits its own bundle (Phase-1 stub + Phase-2 body). The application (or the agent framework — ContextOS is backend-agnostic about *which* one) propagates a `run_id` and `parent_step_id` as request metadata. ContextOS does nothing more than **read** those correlation fields and join the bundles into a tree. Concretely:

- **Spans are READ-ONLY correlation (scope invariant).** ContextOS records the `run_id`/`step_id` linkage in the bundle and renders the tree. It has **no scheduler, no step queue, no retry logic** for agent steps. If step 3 failed, ContextOS shows you *that* it failed and *with what context*; it does not re-run it. Re-execution, if desired, is the application's job — ContextOS will happily replay step 3's context deterministically so the application can decide, but the decision and the action are never ContextOS's.
- **Trace writes are best-effort, sampled, fail-open (C12).** The span-correlation trail follows the trace path: tail-sampled (1–10%) with **force-keep on errors and on any step with cost > $0.05/req**, so expensive or failing agent steps are *always* captured even under sampling. The dollar figures in the tree come from the **billing-grade, fail-closed cost outbox (C12)**, not the sampled traces — so the per-step cost is exact even when the trace itself was sampled out. (In the rare case a step's trace was dropped but its cost record persisted, the tree shows the cost row with a `trace: sampled-out` marker rather than a phantom-free gap.)
- **It is a view over the same substrate.** Clicking any step's `ctx` hash opens the flagship Replay Debugger on that step's bundle. Agent tracing adds *zero* new storage primitives — it is `GROUP BY run_id` over bundles plus a cost-ledger join.

---

## 7. Why these five share one substrate (and why that is the moat)

Every feature above writes into, or reads from, **one artifact**: the content-addressed, per-tenant-encrypted `ContextBundle`, persisted via the two-phase write over Redis Streams (the same stream that is the async plane and the replay log). The payoff is structural:

| Feature | Reads from bundle | Writes to bundle | New storage primitive? |
| --- | --- | --- | --- |
| Replay Debugger (flagship) | everything | — (it *is* the bundle reader) | none |
| Cost-aware downgrade | `route` field + cost ledger | `route` decision | none |
| Cross-user cache sharing | fingerprint + `bundle_cid` audit ref | — | none |
| Memory versioning | candidate provenance | snapshot DAG (same CAS substrate) | none (shares CAS) |
| Context diff | two bundles' deterministic stages | — | none |
| Agent-execution tracing | per-step bundles + `run_id` join | `run_id`/`step_id` correlation | none |

Because there is exactly one substrate, the features **cannot disagree about what happened**. The cost dashboard's dollars, the cache-sharing audit, the diff tool's candidate set, and the agent tree's per-step context are all derived from the same frozen bundles and the same fail-closed cost outbox. A competitor who builds these as five separate logging paths will, eventually and inevitably, show a user four numbers that don't reconcile — and that single moment of "your own tools disagree" is the credibility loss ContextOS is engineered to never suffer.

**Replay is the wedge made tangible: you can hold a request in your hand, frozen and tamper-evident, and re-run every decision that built it.** The substrate is the moat: once every feature is a view over that frozen bundle, adding the sixth, seventh, and eighth feature is a query, not a new pipeline. That asymmetry — one hard substrate, many cheap views — is the entire strategic bet of this section.
