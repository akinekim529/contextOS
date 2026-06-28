# ADR-0003: The Context Replay Debugger Is the Flagship, Built on a Content-Addressed, Per-Tenant-Encrypted Bundle

## Status

Accepted.

## Context

ContextOS does many valuable things — semantic caching, model routing, compression, multi-tenant memory. Several of these are individually monetizable. We must choose **one** flagship capability: the feature that defines the product's identity, gets the deepest engineering investment, and is the reason a serious team adopts ContextOS instead of wiring an LLM SDK directly.

The contenders are **cost-aware model routing** (downgrade 20–40% of model spend on easy queries; part of the 40–65% combined token-cost savings) and the **Context Replay Debugger** (byte-exact replay of every context decision from a content-addressed, per-tenant-encrypted bundle).

Cost-aware routing is *table stakes* — every gateway vendor has a router, and savings are a benchmark war that commoditizes quickly. The thing nobody else has, and the thing that is *uniquely enabled* by ContextOS owning the entire pipeline (auth → cache → retrieve → ACL → compress → assemble → route → adapter → stream → write-back), is the ability to answer the question every production LLM team eventually screams: **"Why did the model see *that* context for *that* request?"**

Today that question is unanswerable. The prompt that hit the backend is a transient, reconstructed-from-logs guess. Retrieval scores, redaction decisions, compression substitutions, packing order, and the tokenizer reserve are all gone the instant the stream closes. ContextOS, because it makes and owns every one of those decisions, can record them and replay them deterministically. That is a category-defining capability, not a feature.

The hard part is the contract: **what is deterministic and what is not**. The backend LLM is non-deterministic; every ContextOS decision around it is deterministic. Conflating the two would make replay either dishonest (claiming to reproduce model output) or useless (refusing to reproduce anything). This is consistency rule **C7**, and it must be one schema used identically across the API, observability, and this flagship.

## Decision

**The Context Replay Debugger is the ContextOS flagship. It performs byte-exact replay of every deterministic ContextOS decision for any past request, sourced from a content-addressed, per-tenant-encrypted replay bundle. Cost-aware routing remains a first-class feature but is explicitly *not* the flagship.**

### The replay bundle (content-addressed, per-tenant-encrypted)

Every request emits one immutable bundle capturing the *inputs and decisions* of each deterministic stage. The bundle is:

- **Content-addressed:** the bundle id is the hash of its canonicalized contents, so identical decision-sets dedupe and any tampering is self-evident. The replay log *is* the Redis Streams async plane (it doubles as the durable record).
- **Per-tenant-encrypted:** payload encrypted under the tenant's key (and per-subject DEK where the bundle contains subject data — within crypto-shred scope), so an RTBF shred makes the bundle irrecoverable along with the underlying memory.
- **Stage-decomposed:** one record per pipeline stage, in pipeline order, so replay can stop/inspect at any stage.

```jsonc
// ReplayBundle — content-addressed, immutable, per-tenant-encrypted at rest.
{
  "bundle_id": "01J...ULID-or-content-hash",     // content address of canonicalized stages[]
  "tenant_id": "01J...",                          // non-null partition key
  "namespace": "project/agent/user",              // C2 hard filter context
  "created_at": "2026-06-28T12:00:00Z",           // RFC-3339 UTC
  "request_fingerprint": "sha256:...",            // coarse cache fingerprint (ADR-0004 / C6)
  "stages": [
    {"stage": "auth_tenant",   "deterministic": true,  "inputs": {...}, "decision": {...}},
    {"stage": "cache_lookup",  "deterministic": true,  "decision": {"tier": "exact|semantic|miss", "hit": false}},
    {"stage": "retrieve",      "deterministic": true,  "decision": {"candidate_ids": [...], "raw_scores": [...]}}, // ADR-0005 raw scores
    {"stage": "acl_redaction", "deterministic": true,  "decision": {"redactions": [...], "namespace_filter": "..."}},
    {"stage": "compression",   "deterministic": true,  "decision": {"blocks": [...], "ratio": 3.2, "nli_pass": true}}, // AFTER acl
    {"stage": "assembly",      "deterministic": true,  "decision": {"selected_ids": [...], "edge_order": [...], "weights": [...]}},
    {"stage": "routing",       "deterministic": true,  "decision": {"model_id": "...", "tokenizer": "...", "reserve": 1024}},
    {"stage": "adapter",       "deterministic": true,  "decision": {"rendered_prompt_hash": "sha256:..."}},
    {"stage": "backend_invoke","deterministic": false, "recorded_output": "<optional materialized output>"}
  ]
}
```

### The replay contract (C7 — one schema, deterministic vs generation boundary)

Replay produces exactly one `ReplayResult` shape, used identically by the API, observability, and this debugger:

```jsonc
// ReplayResult — the SINGLE schema across API + observability + flagship (C7).
{
  "bundle_id": "...",
  "mode": "deterministic | recorded_output | live_backend",
  "stage_results": [ /* per-stage replayed decision + equality verdict vs recorded */ ],
  "output_equal": true,        // ASSERTED only when mode == recorded_output
  "diff": null,                // populated (not byte-equality) when mode == live_backend
  "divergences": []            // any deterministic stage whose replay != recorded => HARD failure
}
```

The boundary is bright-line:

- **Deterministic stages = every ContextOS decision** (auth, cache, retrieve, ACL/redaction, compression, assembly, routing, adapter render). Replaying the bundle reproduces these **byte-exact**. Any divergence is a **bug in ContextOS**, surfaced as a hard `divergence`.
- **`backend.invoke` is non-deterministic.** ContextOS does not own the model; it cannot promise the same tokens.
- **`output_equal` is asserted ONLY in `recorded_output` mode** — when the bundle materialized the backend's response, replay can assert the recorded output matches. This is also what serves the streaming `Idempotency-Key` (C15): the materialized final response is returned with **zero second backend call**.
- **`live_backend=True` yields a *diff*, not byte-equality** — it re-invokes the live model with the byte-exact replayed context and shows what changed, which is precisely the debugging value: same context, different model behavior isolates a model/version issue; different context isolates a ContextOS regression.

### Why this is uniquely a ContextOS capability

Because ContextOS sits between app and backend and owns the ordering invariant, it is the *only* component that observes the raw retrieval scores, the redaction set, the compression substitutions, the packing order, and the tokenizer reserve in one place. A bolt-on logger downstream sees only the final prompt. The flagship is enabled by the architecture, not glued onto it.

## Consequences

**Positive**

- A production team can answer "why did the model see this?" deterministically — the single hardest question in LLM ops becomes a `replay <bundle_id>` command.
- The same bundle powers regression testing (replay old bundles against new ContextOS builds; any deterministic divergence is a caught regression), incident forensics, and the streaming idempotency guarantee (C15) at zero extra backend cost.
- Content-addressing gives free dedupe + tamper-evidence; per-tenant encryption keeps replay within tenant + crypto-shred scope.
- Differentiates ContextOS from every router-only gateway: routing is copied in a quarter; a replay-grade bundle pipeline is not.

**Negative / costs**

- Every deterministic stage must be *replay-pure*: same inputs → same decision, no hidden clock/random/global-state reads. This is a real engineering discipline tax (seeded RNG, injected clocks, version-pinned model metadata) — but it is also what makes the rest of the system testable, so we accept it gladly.
- Bundles cost storage; content-addressing + per-tenant retention policy + RTBF shred bound it.
- `live_backend` replay incurs real model cost; it is explicitly a diff tool, never the default mode.

## Rejected alternatives

| Alternative | Why it fails |
|---|---|
| **Cost-aware routing as the flagship** | Commoditized — every gateway has a router, and savings (the 20–40% downgrade slice of the 40–65% total) become a benchmark race that any competitor can match. It is necessary but not identity-defining. It also doesn't exploit ContextOS's unique position (owning the whole pipeline); replay does. |
| **Assert byte-equality of model output across all replays** | Dishonest — the backend is non-deterministic; ContextOS does not own it. Asserting output equality outside `recorded_output` mode would make the tool lie. C7 forbids it: `output_equal` is asserted *only* for recorded-output replay. |
| **Refuse to replay anything non-deterministic (deterministic-only, no live mode)** | Throws away the highest-value debugging case: re-running the *same* byte-exact context against a *live* model to isolate whether a regression is in ContextOS or in the model/version. We keep this as `live_backend` *diff* mode rather than dropping it. |
| **Reconstruct context from logs/traces instead of a dedicated bundle** | Logs are lossy and reconstructed — raw retrieval scores, redaction decisions, and packing order are gone or approximated. Reconstruction yields a *guess*, not byte-exact replay, defeating the entire premise. Traces are best-effort and sampled (C12), unfit as the source of truth. |
| **Store the final rendered prompt only (no per-stage decomposition)** | You can see *what* the model got but never *why* — you cannot inspect at the retrieval or compression stage, cannot attribute a bad context to a redaction vs a packing bug. The per-stage bundle is what makes it a *debugger* and not a prompt archive. |
| **Plaintext / non-content-addressed bundles** | Plaintext breaks tenant isolation and RTBF crypto-shred; non-content-addressed loses tamper-evidence and dedupe and complicates the idempotency guarantee. Both are strictly worse with no upside. |

## Cross-section assumptions

- The pipeline ordering invariant (auth → cache → retrieve → ACL/redaction → compression → assembly → routing → adapter → stream → write-back) is the exact stage order recorded in `stages[]`; compression is recorded **after** ACL/redaction.
- `retrieve` records **raw per-modality scores** and `assembly` records the final selection + edge order + weights — consistent with ADR-0005 (Memory returns raw scores, Assembler ranks/packs).
- `routing` records the selected `model_id` + `tokenizer` + hard `reserve` *before* the adapter render, consistent with C3 (router picks the model before final packing).
- `request_fingerprint` is the COARSE cache fingerprint from ADR-0004 (C6); memory-private-grounded responses are flagged non-cacheable and their bundles reflect that flag.
- The `recorded_output` mode is the same materialized response that serves the streaming `Idempotency-Key` with zero second backend call (C15).
- Bundles are per-tenant-encrypted and within crypto-shred scope (C11); RTBF shred renders the bundle irrecoverable.
