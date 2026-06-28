# ADR-0004: COARSE Cache Fingerprint, Memory-Private-Grounded Responses Non-Cacheable, 25–45% Hit-Ratio Target

## Status

Accepted.

## Context

ContextOS runs a two-tier semantic cache: an **exact-hash tier in Redis** (`< 1 ms p99`) and a **semantic-ANN tier in pgvector/Qdrant** (`8–15 ms p95`, includes query embedding on miss), both per-tenant namespaced. Caching contributes the **15–30%** slice of the **40–65%** combined token-cost savings, so the cache is real money, not a micro-optimization.

The central design question is the **cache key (fingerprint)**: what must be identical for two requests to be considered the "same" cached response? This is a precision/recall trade-off with a sharp safety edge:

- **Too fine** a fingerprint (hash the entire literal prompt including assembled context) → the cache almost never hits, because retrieval candidates and packing order vary request-to-request. Hit-ratio collapses toward zero and the cache pays for itself never.
- **Too coarse** a fingerprint (e.g., just the bare user query) → the cache *over-hits* and serves a response that was grounded in **different private memory** or a **different model**, leaking one user's grounded answer to another context. That is a correctness *and* isolation failure.

The hardest case is **memory-private-grounded responses**: an answer whose content was synthesized from a specific user's/tenant's private retrieved memory. Two superficially identical queries from two principals can have legitimately different correct answers because their private grounding differs. Caching such a response by query similarity is actively dangerous.

This is consistency rule **C6**: a COARSE fingerprint defined over stable, safe-to-share signals; memory-private-grounded responses flagged non-cacheable; a realistic hit-ratio re-derived to **25–45%**; an offline cache-correctness eval harness as a roadmap item.

## Decision

**The cache fingerprint is COARSE, computed over four stable signals; memory-private-grounded responses are flagged NON-CACHEABLE; the realistic hit-ratio target is 25–45% on a mixed workload.**

### The COARSE fingerprint

```text
coarse_fingerprint = hash(
      normalized_query_embedding_bucket    # bge-small-en-v1.5 (384-dim), L2-normalized, then bucketed
    + model_id                             # selected backend identity
    + system_prompt_version                # versioned, not raw text
    + stable_fact_set_version              # version of the shared/stable grounding facts
)
```

Each component is deliberate:

| Component | Why it is in the fingerprint | Why it is *coarse* |
|---|---|---|
| `normalized_query_embedding_bucket` | Semantic equivalence: "reset my password" ≈ "how do I reset password". | **Bucketed**, not exact: the L2-normalized 384-dim embedding is quantized into ANN buckets so near-identical queries share a key. Exact-tier additionally hashes the *literal* normalized query string for the `< 1 ms` lookup. |
| `model_id` | A cached answer is only valid for the model that produced it; a downgraded/upgraded model is a different response. | Identity string, not weights/version-minutiae. |
| `system_prompt_version` | A new system prompt changes behavior; stale cache would serve old policy. | **Version**, not raw prompt text — so prompt whitespace churn doesn't bust the cache, only a real version bump does. |
| `stable_fact_set_version` | Responses grounded in *shared, stable* facts are reusable; a version bump invalidates them en masse. | Version stamp on the shared fact set only — **private** grounding is excluded entirely (see below). |

### Two tiers, two precisions

- **Exact tier (Redis, `< 1 ms p99`):** key = `{tenant_id}:{namespace}:exact:{hash(literal_normalized_query + model_id + system_prompt_version + stable_fact_set_version)}`. Byte-identical normalized query. The `< 1 ms` claim applies **only** here.
- **Semantic tier (pgvector/Qdrant, `8–15 ms p95`):** key namespace `{tenant_id}:{namespace}:semantic`, lookup by `normalized_query_embedding_bucket` ANN within the COARSE fingerprint partition (same `model_id` + `system_prompt_version` + `stable_fact_set_version`). The 8–15 ms includes the ~6 ms query embedding on a miss.

### Non-cacheable flag (the safety edge)

A response is **flagged non-cacheable** and bypasses both write paths when it is **memory-private-grounded** — i.e., its content was materially synthesized from a principal's/tenant's *private* retrieved memory rather than from the shared, versioned `stable_fact_set`. The assembler emits a `grounded_private: bool` signal (true iff any selected candidate came from a private memory namespace). The cache write-back honors it:

```python
def cache_writeback(resp, fingerprint, grounded_private: bool):
    if grounded_private:
        # NON-CACHEABLE: private grounding makes the answer principal-specific.
        # Recording it risks serving one principal's grounded answer in another context.
        metrics.incr("cache.skip.private_grounded")
        return  # fail-closed: do not cache
    cache.put(fingerprint, resp)   # safe: grounded only in shared, versioned facts
```

This is fail-closed: when in doubt about grounding provenance, *do not cache*. Per-tenant namespacing already prevents cross-tenant reads; the non-cacheable flag prevents the subtler *within-tenant, cross-principal* mis-grounding.

### Hit-ratio target (re-derived)

The realistic, defensible target is **25–45% on a mixed workload**. This is *lower* than a naive "cache everything" pitch precisely because the COARSE policy excludes private-grounded responses and partitions by model/system-prompt/fact-set version. We commit to the honest number. An **offline cache-correctness eval harness** (replay recorded requests, assert that every served hit was a *legitimate* hit by re-grounding) is a **roadmap item** that gates any future loosening of the fingerprint.

## Consequences

**Positive**

- The COARSE fingerprint hits often enough (25–45%) to deliver the 15–30% caching slice of savings, without the literal-prompt fingerprint's near-zero hit rate.
- The non-cacheable flag eliminates the worst correctness failure (serving one principal's privately-grounded answer to another), making the cache safe to enable by default.
- Versioned components (`system_prompt_version`, `stable_fact_set_version`) give *en-masse, instant* invalidation on a bump — no key sweeping.
- Two-tier design keeps the common exact case at `< 1 ms` while the semantic tier catches paraphrases at 8–15 ms.

**Negative / costs**

- Private-grounded traffic is uncacheable, which *caps* the achievable hit-ratio — hence 25–45%, not 70%. We accept a lower ceiling for safety; this is the right trade.
- The COARSE bucket can theoretically collide two semantically-distinct-but-close queries; the semantic tier mitigates by returning a similarity score and applying a threshold before serving (sub-threshold = treat as miss).
- We owe the offline correctness harness before loosening the fingerprint; until it exists, the fingerprint does not get finer.

**Operational**

- Hit-ratio, private-grounded skip rate, and semantic-tier sub-threshold miss rate are first-class metrics; a hit-ratio outside 25–45% triggers fingerprint review, not silent drift.

## Rejected alternatives

| Alternative | Why it fails |
|---|---|
| **FINE fingerprint = hash the full literal assembled prompt (query + packed context)** | Retrieval candidates, MMR selection, and packing order vary request-to-request, so the full-prompt hash almost never repeats. Hit-ratio collapses toward zero; the cache costs storage and lookups while saving nothing. Defeats the 15–30% savings target. |
| **Bare-query fingerprint (query only, ignore model/prompt/fact-set version)** | Over-hits catastrophically: serves a response generated by a *different model*, under a *different system prompt*, or grounded in a *stale fact set*. Worst case, serves a privately-grounded answer to the wrong principal. A correctness and isolation failure. |
| **Cache memory-private-grounded responses too (no non-cacheable flag)** | Two principals with similar queries but different private memory get the *same* cached answer — one principal's grounded answer leaks into another's context. This is the precise failure C6 exists to prevent; excluding private-grounded traffic is mandatory. |
| **Exact-tier only (no semantic ANN tier)** | Misses every paraphrase ("reset password" vs "how to reset my password"), leaving most of the realistic hit opportunity on the table and dragging the hit-ratio below the 25–45% band. The semantic tier is what captures natural-language variation. |
| **Semantic-tier only (no exact Redis tier)** | Every lookup pays the 8–15 ms semantic path even for byte-identical repeat queries that could be answered in `< 1 ms`. Wastes the cheapest, highest-confidence hits and inflates p95 for the common case. |
| **Claim a 60–70% hit-ratio in the design** | Dishonest given the COARSE + non-cacheable policy. The defensible re-derived figure is 25–45%; over-promising sets a target the safe policy cannot hit and pressures loosening the fingerprint past the safety edge. |

## Cross-section assumptions

- The fingerprint embedding is BAAI/bge-small-en-v1.5 (384-dim, ~6 ms p95 query embed) — the same canonical embedder used everywhere; no second embedding model.
- `< 1 ms p99` applies **only** to the exact Redis tier; the semantic tier is `8–15 ms p95` (C5) — no section may state sub-1ms for the semantic tier.
- Both tiers are per-tenant namespaced by `{tenant_id}:{namespace}` consistent with ADR-0002 isolation; the non-cacheable flag handles the *within-tenant, cross-principal* case that namespacing alone does not.
- `grounded_private` is emitted by the Context Assembler (ADR-0005 owns final selection and therefore knows whether any selected candidate is from a private namespace).
- Caching contributes the 15–30% slice of the 40–65% combined token-cost savings; the 25–45% hit-ratio and that savings slice are the canonical figures every section must reuse.
- `model_id` and `system_prompt_version` in the fingerprint align with the router selecting the model before final packing (C3) and the replay bundle's recorded routing decision (ADR-0003).
