# ADR-0005: Memory Returns Raw-Scored Candidates; the Context Assembler Is the Sole Final-Ranking and Budget-Packing Authority

## Status

Accepted.

## Context

Two subsystems sit on the retrieval path and both *could* plausibly own "ranking":

- The **Memory Engine** runs the retrieval subsystem — query-embed (`~6 ms`), pgvector HNSW ANN (`18 ms p95`) in parallel with BM25, RRF fuse, and rescore — under a `< 100 ms p95` SLO (hot path ~40 ms). It produces candidates with per-modality signals: vector similarity, lexical (BM25) score, memory-decay **recency**, and a quality/confidence score.
- The **Context Assembler** runs the `< 50 ms p95` stage: final scoring over `<= 512` candidates, MMR diversity, a budget knapsack against the hard token reserve, and lost-in-the-middle edge placement.

If *both* apply ranking weights, we get two weight vocabularies, two places where "what matters" is encoded, and an impossible-to-debug interaction: Memory pre-ranks by its weights, then Assembler re-ranks by *its* weights over an already-distorted ordering. Worse, the two notions of "importance" are different in kind: Memory's **recency** is a property of a memory item (how fresh is this fact), while the Assembler's **edge placement** is a property of the *prompt layout* (lost-in-the-middle: put the most relevant items at the head and tail of the context window). Folding these into one ranking conflates orthogonal concerns and makes the replay bundle (ADR-0003) ambiguous about *who decided what*.

This is consistency rule **C1**: Memory returns candidates with **raw per-modality scores only**; the Context Assembler is the **sole** final-ranking and budget-packing authority; there is **one weight vocabulary**; memory-decay recency is **orthogonal** to assembler lost-in-the-middle ordering.

## Decision

**The Memory Engine returns candidates carrying RAW per-modality scores and NO final ranking. The Context Assembler holds the single weight vocabulary and is the sole authority for final ranking, MMR, and budget packing.**

### Contract: what Memory returns

Memory fuses (RRF) only to *select which candidates survive* into the `<= 512` cap; it does **not** collapse the per-modality signals into a single rank for the Assembler. Each surviving candidate carries its raw signals intact.

```python
@dataclass(frozen=True)
class RetrievedCandidate:
    id: str                       # ULID
    tenant_id: str                # non-null partition key
    namespace: str                # C2 hard-filtered already at repo boundary
    embedding: list[float]        # 384-dim, bge-small-en-v1.5
    token_count: int
    # RAW per-modality scores ONLY — Memory does NOT pre-rank for the assembler:
    raw_vector_score: float       # pgvector cosine similarity
    raw_bm25_score: float         # lexical
    raw_recency_score: float      # memory-decay (a property of the item, NOT prompt layout)
    raw_quality_score: float      # source confidence / quality
    rrf_rank_for_selection: int   # used ONLY to choose the <=512 survivors, not as the final weight
```

`rrf_rank_for_selection` exists solely to bound the candidate set to `<= 512` (the hard cap, ADR-0006). It is *not* the final ranking and the Assembler does not treat it as one.

### Contract: what the Assembler owns

The Assembler holds the **one weight vocabulary** `W = [w_vector, w_bm25, w_recency, w_quality]` (the single source of truth; the kernel in ADR-0001 consumes exactly this vector). It computes the final relevance score, runs MMR for diversity, packs the knapsack against the hard token reserve, and *then* places by lost-in-the-middle.

```python
def assemble(cands: list[RetrievedCandidate], W, mmr_lambda, token_budget):
    # 1. FINAL relevance score — the ONE weight vocabulary lives here and ONLY here.
    for c in cands:
        c.final_score = (W.vector  * c.raw_vector_score
                       + W.bm25    * c.raw_bm25_score
                       + W.recency * c.raw_recency_score      # recency folded in HERE, once
                       + W.quality * c.raw_quality_score)
    # 2. MMR: relevance vs diversity over <=512 candidates (iterative).
    selected = mmr(cands, key="final_score", lam=mmr_lambda)
    # 3. Budget knapsack against the HARD token reserve (router already chose tokenizer, C3).
    packed = knapsack(selected, token_budget)
    # 4. Lost-in-the-middle edge placement — ORTHOGONAL to recency: this is PROMPT LAYOUT,
    #    re-ordering the packed set so highest final_score sits at head & tail of the window.
    return edge_place(packed)
```

**Recency vs edge-placement are kept orthogonal on purpose:** recency is a *content* signal weighted at step 1 (how fresh is the fact); edge-placement is a *layout* decision at step 4 (where in the window to put a chosen item so the model attends to it). A fresh-but-mid-ranked item is *not* automatically placed at an edge, and an edge slot is *not* reserved for the most recent item. Conflating them would let recency hijack layout, which is wrong.

## Consequences

**Positive**

- **One** place encodes "what matters" (the weight vocabulary), so tuning relevance is a single, debuggable knob, not a tug-of-war between two subsystems.
- The replay bundle (ADR-0003) is unambiguous: `retrieve` records **raw** scores; `assembly` records the **final** selection + weights + edge order. Anyone replaying sees exactly who decided what.
- Memory can evolve its fusion/selection (RRF tuning, new modality) without touching ranking semantics, and the Assembler can re-tune weights without a Memory change. Clean seam.
- Keeping recency and edge-placement orthogonal prevents the subtle "newest item always at the top" failure that hurts answer quality.

**Negative / costs**

- Memory ships *more* data per candidate (four raw scores instead of one fused rank). At `<= 512` candidates × a few floats this is negligible on the wire and within the 40 ms retrieval hot-path budget.
- The Assembler must always be in the path to produce a final ranking — there is no "Memory already ranked it" shortcut. This is intentional: ranking has exactly one owner.

**Operational**

- The weight vocabulary `W` is versioned and recorded in the replay bundle, so a relevance regression is bisectable to a weight change.

## Rejected alternatives

| Alternative | Why it fails |
|---|---|
| **Memory pre-ranks with its own weights, Assembler re-ranks** | Two weight vocabularies → two encodings of "importance" that interact unpredictably. The Assembler re-ranks an already-distorted order, making relevance impossible to tune or debug, and the replay bundle can't attribute a decision to one owner. Violates C1's "one weight vocabulary." |
| **Memory returns a single fused score; Assembler ranks on that scalar only** | Discards the per-modality signals the Assembler needs — it cannot re-weight vector vs lexical vs recency vs quality once they're collapsed. Compression/diversity decisions that depend on *why* a candidate ranked (lexical match vs semantic) become impossible. The scalar is lossy. |
| **Fold lost-in-the-middle edge placement into the recency score** | Conflates a *content* signal (freshness) with a *layout* decision (where in the window). Recent-but-irrelevant items would be hoisted to edge slots, degrading the very lost-in-the-middle benefit edge-placement exists to capture. C1 mandates these stay orthogonal. |
| **Assembler delegates packing back to Memory (Memory owns knapsack)** | The knapsack must run against the *hard token reserve*, which depends on the routed model's tokenizer — known only after routing (C3), which is downstream of Memory. Memory cannot pack correctly because it doesn't know the tokenizer yet. Packing must live with the Assembler. |
| **A shared ranking service both call** | Adds a third component and a network hop into two stages whose combined budget is tight (`<100ms` retrieve + `<50ms` assemble), and re-introduces the "who owns the weights" ambiguity at the service boundary. The Assembler already owns ranking; a separate service is needless surface area. |

## Cross-section assumptions

- The `<= 512` candidate cap is the hard scope-boundary invariant from ADR-0006; `rrf_rank_for_selection` only enforces that cap and never substitutes for final ranking.
- The single weight vocabulary `W` is exactly the `weights` vector consumed by the provisional `AssemblerKernel` in ADR-0001; there is no second weight store anywhere.
- The knapsack packs against the hard token reserve that the router established by selecting the model/tokenizer *before* final packing (C3); the Assembler never re-derives the reserve.
- `retrieve`/`assembly` stages in the ADR-0003 replay bundle record raw scores and final selection respectively, matching the contract here.
- Retrieval stays within `< 100 ms p95` (hot path ~40 ms) and assembly within `< 50 ms p95` — the canonical figures; this ADR adds no new latency claim.
- `grounded_private` for the cache non-cacheable flag (ADR-0004) is derived by the Assembler from whether any *selected* candidate's `namespace` is private — only the Assembler knows the final selection.
