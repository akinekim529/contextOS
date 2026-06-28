# ADR-0006: Scope Boundaries — ≤512 Pre-Retrieved Candidates, Never Build/Own an Index, Agent Spans Read-Only, GPU Routing Telemetry-Only

## Status

Accepted.

## Context

ContextOS is **middleware** between applications and any LLM backend. It owns memory, context assembly under a token budget, multi-tenant isolation, semantic caching, model routing, and replay-grade observability. The single greatest *strategic* risk to the project is **scope drift**: the slow, well-intentioned slide where ContextOS reimplements a vector database, an inference engine, a training system, or an agent framework because "we're already so close." Every such drift bloats the surface area, breaks the lightweight-middleware value proposition, and pits ContextOS against far better-resourced specialized systems it should *integrate with*, not *become*.

The features most prone to drift are precisely the most interesting ones:

- **Retrieval scoring/MMR** is one refactor away from "let's just maintain our own index."
- **Agent-trace correlation** is one feature request away from "let's re-execute the failed step."
- **GPU-aware routing** is one optimization away from "let's schedule the GPUs ourselves."
- **Memory consolidation** is one loop away from "let's make it an autonomous agent."

These are not hypothetical; they are the gravitational pull of an ambitious middleware. This ADR makes the boundaries **explicit, named invariants** so a reviewer can reject a drifting PR by citing a number and a rule, not a vibe. This is consistency rule **C13** (and it backstops the C1/ADR-0005 retrieval split, the C14/ADR-0001 kernel gate, and the routing posture rules).

## Decision

**ContextOS is NOT an LLM, an inference engine, a vector DB, a training system, or an agent framework. The following invariants are binding and any PR that crosses one is rejected on sight.**

### Invariant 1 — In-process scoring/MMR over pre-retrieved candidates only; NEVER build or own an index

- Final scoring, MMR, and budget packing operate on a **hard cap of ≤ 512 pre-retrieved candidates**, in-process (ADR-0005).
- ContextOS **never builds, owns, maintains, or rebalances a vector index.** The index lives in pgvector (HNSW, `18 ms p95` probe at `≤ 5M vectors/tenant`) or Qdrant (escape hatch beyond that), behind a `VectorStore` adapter.
- Any **cross-encoder reranker is OPT-IN and out-of-band only** — never a default, never inline on the hot path. The default embedder is BAAI/bge-small-en-v1.5 (384-dim); the cross-encoder is a separately-invoked, opt-in step.

```python
# The cap is enforced at the boundary, not by convention. Crossing it is a programming error.
MAX_CANDIDATES = 512  # scope-boundary invariant: ContextOS never owns an index.

def into_assembly(candidates: list[RetrievedCandidate]) -> list[RetrievedCandidate]:
    if len(candidates) > MAX_CANDIDATES:
        # Fail loud — this means someone tried to make ContextOS a search engine.
        raise ScopeBoundaryViolation(
            f"{len(candidates)} candidates > {MAX_CANDIDATES} hard cap; "
            "ContextOS scores pre-retrieved candidates, it does not build an index."
        )
    return candidates
```

### Invariant 2 — Agent-trace spans are READ-ONLY correlation

- ContextOS ingests agent-trace spans for **observability correlation only**. It correlates spans to requests, replay bundles, and cost records.
- It **never schedules, re-executes, retries, or re-orders** agent steps. There is no execution authority over agent traces — read-only, full stop. Wanting to "just re-run the failed step" is the agent-framework drift this invariant forbids.

### Invariant 3 — Memory consolidation is an async, rate-limited, COST-TRACKED batch job — not an agent loop

- Memory consolidation (summarize/compress/promote long-term memory) runs as an **async, rate-limited batch job**, not an autonomous agent loop.
- Its inference cost is **tracked and enters the budget ledger** like any other model spend (billing-grade, fail-closed durable outbox, C12). A consolidation job that wants to spend must charge the ledger; it is not free background magic.

### Invariant 4 — GPU-aware routing is a telemetry READER, never a scheduler

- The router may **read** GPU/queue/latency telemetry to inform optimization signals.
- It **never schedules, allocates, reserves, or places work on GPUs.** GPU scheduling belongs to the inference platform ContextOS routes *to*, not ContextOS.
- This composes with the router fail-posture: hard-policy filters (allowlist, residency, capability, budget) evaluate on **static** policy and fail-**closed** independent of any health/telemetry store; only optimization signals (latency/queue/quality) fail-**open** to static ranking. GPU telemetry is purely an optimization signal — its absence degrades to static ranking, never to a scheduling decision.

### Enforcement

These invariants are not aspirational comments; they are enforced:

- The `MAX_CANDIDATES` boundary raises `ScopeBoundaryViolation` in code and is asserted in tests.
- A PR-template checklist item ("Does this build/own an index? schedule agent steps? schedule GPUs? turn consolidation into a loop?") gates review.
- The architecture document states these invariants verbatim so any drift is a documented contract violation, not a judgment call.

## Consequences

**Positive**

- ContextOS stays a *lightweight, composable middleware* — it integrates with the best vector DB, inference platform, and agent framework rather than competing badly with all three.
- The `≤ 512` cap keeps the assembly hot path bounded and the `< 50 ms p95` budget achievable; an unbounded candidate set would make the budget meaningless.
- Read-only agent spans + cost-tracked consolidation + telemetry-only GPU routing keep the system's behavior *predictable and auditable* — nothing autonomous, nothing that schedules, nothing untracked.
- Reviewers can reject drift mechanically by citing an invariant, which keeps scope decisions out of opinion territory.

**Negative / costs**

- Some users will want ContextOS to "just also be" their reranking index or agent scheduler. We say no and point them to pgvector/Qdrant adapters and their agent framework. We accept lost "do-everything" appeal in exchange for a sharp, defensible product.
- The `≤ 512` cap means very-large-recall use cases must do coarse retrieval upstream (in the vector DB) before handing candidates to ContextOS. This is correct: index-scale recall is the vector DB's job.

**Operational**

- `ScopeBoundaryViolation` surfacing in production is a *bug report about drift*, not a tuning issue — it means a caller tried to use ContextOS as something it is not.

## Rejected alternatives

| Alternative | Why it fails |
|---|---|
| **Let ContextOS build/own its own vector index for "better" retrieval** | Makes ContextOS a vector DB — exactly the scope it must not occupy. It would compete with pgvector/Qdrant (mature, faster, index-specialized), own index build/rebalance/HA, and break the lightweight-middleware proposition. The `VectorStore` adapter + `18 ms p95` HNSW probe already gives us what we need without owning an index. |
| **Raise/remove the ≤512 candidate cap for high-recall cases** | An unbounded in-process candidate set turns assembly into a search engine and blows the `< 50 ms p95` budget. High recall is the upstream vector DB's job; ContextOS scores the *survivors*. The cap is the line between "middleware that ranks candidates" and "a search engine." |
| **Make agent-trace spans actionable (retry/re-execute failed steps)** | Turns ContextOS into an agent framework with execution authority over someone else's agent — the precise scope boundary forbidden. It would couple ContextOS to agent semantics it doesn't own and create a second scheduler competing with the user's framework. Spans stay read-only correlation. |
| **Run memory consolidation as an autonomous agent loop** | An agent loop is unbounded, hard to cost-attribute, and is itself agent-framework drift. The batch job is rate-limited, cost-tracked into the budget ledger, and deterministically schedulable — auditable where a loop is not. |
| **Let the router schedule/place work on GPUs for optimal utilization** | Makes ContextOS an inference scheduler — out of scope and in conflict with the inference platform's own scheduler. The router reads telemetry to *choose a backend*; the backend's platform schedules its GPUs. Crossing this makes ContextOS responsible for hardware placement it cannot safely own. |
| **Make the cross-encoder reranker a default inline hot-path step** | A cross-encoder is heavy and would blow the retrieval/assembly budgets and re-introduce index-like ownership pressure. Keeping it opt-in/out-of-band preserves the budgets and the boundary; teams that want it invoke it deliberately, off the hot path. |

## Cross-section assumptions

- `≤ 512` is the single canonical candidate cap shared with ADR-0005 (ranking ownership) and ADR-0001 (the kernel benchmarks the same `≤512` distribution); no section uses a different cap.
- The vector index is pgvector HNSW (`18 ms p95` at `≤ 5M vectors/tenant`, Qdrant beyond) behind a `VectorStore` adapter — the single source of truth for vector-query latency; ContextOS owns the adapter, never the index.
- The router fail-posture (hard filters static + fail-closed; optimization signals fail-open to static ranking) is the C9 posture; GPU telemetry is one such optimization signal and its loss degrades to static ranking, never to a scheduling action.
- Memory-consolidation inference cost enters the same budget ledger that billing-grade cost records (C12, fail-closed durable outbox) maintain.
- The default embedder is BAAI/bge-small-en-v1.5 (384-dim); the cross-encoder reranker is opt-in/out-of-band, consistent with the canonical facts.
