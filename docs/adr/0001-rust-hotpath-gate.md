# ADR-0001: Provisional Rust/PyO3 Hot-Path Kernel Behind a Benchmark Gate

## Status

Accepted (the Rust kernel itself is **PROVISIONAL** — this ADR accepts the *gate*, not the Rust port).

## Context

ContextOS is a Python 3.11+ asyncio control plane (uvloop event loop, FastAPI/Starlette on Uvicorn). The control plane is overwhelmingly I/O-bound: it awaits Postgres, Redis, pgvector, the embedding service, and the model backend. For that work, asyncio + uvloop is the correct tool and Rust would buy nothing.

The exception is the **Context Assembler hot path** — the only CPU-bound stage ContextOS owns. Per the canonical latency budget it must complete in **< 50 ms p95** for up to **512 pre-retrieved candidates**, doing:

1. Final scoring over `<= 512` candidates (one weight vocabulary; see ADR-0005),
2. MMR diversity selection,
3. A budget knapsack pack against the hard token reserve,
4. Lost-in-the-middle edge placement.

This is tight numeric/array work over 384-dim embeddings (BAAI/bge-small-en-v1.5). At the gateway target of **5k–10k req/s per node** (~0.7 vCPU per 1k req/s, hot-path CPU-bound), this stage is the single most likely place where CPython's interpreter overhead and the GIL become the binding constraint. NumPy vectorization carries us a long way, but MMR is inherently iterative (each pick depends on prior picks), and Python-level loops over 512 candidates × selection rounds are exactly where CPython falls down.

We must decide *now* whether to commit to a Rust/PyO3 kernel. Committing prematurely imports a second toolchain, a second memory model, an FFI boundary, and a hiring constraint, for a stage that may comfortably fit the budget in pure Python. Refusing to plan for it risks a late, panicked rewrite under SLA pressure. The answer is a **gate**: define the exact benchmark, the exact trigger threshold, and the owner, so the decision is mechanical and unambiguous rather than political.

## Decision

**The Context Assembler ships in pure Python (NumPy-vectorized scoring/MMR/knapsack) at launch. A Rust/PyO3 reimplementation of the assembler kernel is PROVISIONAL and is triggered ONLY when the benchmark below crosses a fixed p95 threshold with insufficient headroom.**

### The gate benchmark (canonical, reproducible)

The benchmark is a committed, CI-runnable artifact (`benchmarks/assembler_gate.py`) that exercises the *real* `ContextAssembler.assemble()` code path — not a microbenchmark of a synthetic loop.

| Benchmark dimension | Fixed value |
|---|---|
| Candidate-count distribution | Mixed, weighted to stress the cap: **p50 = 128, p90 = 384, p99 = 512** candidates per request (truncated at the `<= 512` hard cap) |
| Embedding dimensionality | **384** (BAAI/bge-small-en-v1.5), float32, L2-normalized |
| Per-candidate payload | embedding(384·f32) + 4 raw per-modality scores (vector/BM25/recency/quality) + token_count(u16) + ULID |
| Target throughput | **8,000 req/s sustained** (mid-band of the 5k–10k node target), single node |
| Concurrency model | Assembler invoked from the uvloop executor pool; measured under realistic GIL contention with concurrent I/O coroutines live |
| Token budget per request | 8,192-token assembly window (forces a non-trivial knapsack) |
| Warm-up / measurement | 30 s warm-up discarded; 300 s measurement window; report p50/p95/p99 |
| Hardware reference | 1 vCPU pinned to the assembler (the ~0.7 vCPU/1k-req/s envelope scaled to the run) |

### The trigger

The canonical assembly SLO is **< 50 ms p95**. The gate splits that budget and reserves headroom:

- **Assembler-internal p95 budget: 35 ms** (the remaining 15 ms of the 50 ms is reserved for serialization, GC pauses, scheduler jitter, and tail amplification under load).
- **Trigger condition (Rust port is opened):** measured **assembler-internal p95 ≥ 40 ms** on the gate benchmark (i.e., it has consumed ≥ 80% of the 50 ms SLO and breached the 35 ms internal budget with < 15 ms headroom), **OR** sustaining the 8,000 req/s target drives single-node assembler CPU above **85% of one vCPU** such that the gateway can no longer hold 5k–10k req/s.
- **Hold condition (stay in Python):** assembler-internal p95 **≤ 35 ms** with **≥ 30% headroom** to the 50 ms SLO.
- **Watch band (35–40 ms):** no port; profile and optimize Python (vectorize the remaining MMR loop, pre-allocate buffers, switch to a structured `float32` candidate matrix, consider `numba` on the MMR inner loop as a *non-Rust* escape valve before invoking the gate).

The gate is evaluated on every release candidate. Two consecutive RCs in the trigger band open the Rust ADR-0001-R (the port), not before.

### Owner

**Owner: the Hot-Path / Performance lead** (the engineer who owns the `ContextAssembler` module and the latency budget in Section 9). They own (a) keeping `benchmarks/assembler_gate.py` honest and in CI, (b) declaring trigger/hold/watch each RC, and (c) if triggered, owning the Rust kernel behind the `ContextAssembler` interface. The gate result is a required, signed line in the release checklist.

### The PROVISIONAL Rust interface

If triggered, Rust replaces *only* the inner kernel behind a stable Python protocol. The boundary is the data structure, so the rest of ContextOS never learns the kernel changed. **This interface is PROVISIONAL** — field names/types may change until ADR-0001-R is accepted.

```python
# PROVISIONAL — subject to ADR-0001-R. Do not depend on exact field layout yet.
from typing import Protocol
import numpy as np

class AssemblerKernel(Protocol):
    """Pure-CPU kernel: final-rank + MMR + knapsack + edge-place.
    Pure Python (NumPy) at launch; PyO3 implementation swaps in iff the gate triggers.
    No I/O, no logging, no allocation surprises — deterministic given inputs.
    """
    def assemble(
        self,
        embeddings: np.ndarray,      # shape (n<=512, 384) float32, L2-normalized
        raw_scores: np.ndarray,      # shape (n, 4) float32: [vector, bm25, recency, quality]
        token_counts: np.ndarray,    # shape (n,) uint16
        weights: np.ndarray,         # shape (4,) float32 — the ONE weight vocabulary (ADR-0005)
        mmr_lambda: float,           # diversity/relevance trade-off
        token_budget: int,           # hard reserve already subtracted by router (ADR C3)
    ) -> tuple[np.ndarray, np.ndarray]:
        # returns (selected_indices: int32[k], edge_order: int32[k])
        ...
```

The PyO3 build is feature-gated (`contextos[rust-kernel]`), distroless-compatible (musl/static-link the `.so`), and selected at startup by a single flag. CI runs the **identical** assembler property/golden tests against both implementations; byte-identical selection output (same indices, same edge order) is a merge gate — divergence fails the build.

## Consequences

**Positive**

- Launch ships on one toolchain (Python + uv + Hatchling). No premature Rust tax on hiring, build, or debuggability.
- The decision to add Rust is *mechanical and owned*, not a hallway argument: a number on a committed benchmark flips it.
- The 15 ms headroom inside the 50 ms SLO means we trigger the port *before* customers see breaches, not after.
- Because the swap is behind `AssemblerKernel`, the rest of the pipeline (ordering invariant, ADR-0005 ranking ownership) is untouched by the kernel choice.

**Negative / costs**

- The PyO3 interface is PROVISIONAL, so anyone tempted to reach into kernel internals early is explicitly told not to. We accept a small "do not touch yet" friction.
- We carry a dual-implementation test obligation *if* triggered (golden-output parity on every change). That is real maintenance, justified only when the gate fires.
- A `numba` watch-band escape valve adds a *third* possible code path; we bound this by forbidding `numba` in shipped code unless it, too, passes the golden-parity gate.

**Operational**

- The gate line is mandatory in the release checklist; a release cannot ship "unknown" — the owner must declare trigger/hold/watch with the benchmark artifact attached.

## Rejected alternatives

| Alternative | Why it fails |
|---|---|
| **Port the assembler to Rust/PyO3 now (eager)** | Buys nothing measurable until CPython is proven to be the constraint, while importing a second toolchain, an FFI memory model, distroless static-link complexity, and a narrower hiring pool. Optimizing a stage that already fits < 50 ms p95 is speculative cost. The gate exists precisely to refuse this. |
| **Never use Rust; pure Python forever** | MMR is iterative (each pick depends on prior selections), so it does not fully vectorize. At 8k req/s with the GIL contended by live I/O coroutines, the 512-candidate tail can blow the 50 ms p95. Refusing Rust on principle risks a late, unowned rewrite under SLA fire. |
| **Cython instead of Rust/PyO3** | Cython removes interpreter overhead but keeps CPython's memory model and GIL semantics, and still compiles against the CPython ABI — fragile under distroless and across Python minor versions. It gives weaker guarantees than a `release`-mode Rust kernel that drops the GIL for the compute window, for comparable porting effort. We keep `numba` (JIT, no separate toolchain) as the watch-band optimizer instead. |
| **Push assembly into Postgres/SQL (do scoring/MMR server-side)** | Violates the scope-boundary invariant (ADR-0006): MMR and knapsack are in-process ranking over pre-retrieved candidates, not a database index operation. It would also couple assembly latency to DB load and break the clean `<= 512` in-memory contract. |
| **Run the kernel as a separate gRPC microservice** | Adds a network hop (serialization + RTT) to a stage whose entire budget is 50 ms. The ordering invariant keeps assembly *in-process at launch* exactly to avoid this; a remote kernel would consume the headroom we are trying to protect. |
| **GPU-accelerate the assembler** | The hot path is 512×384 float math — trivially CPU-cacheable and latency-dominated by per-request dispatch, not throughput. GPU dispatch latency exceeds the whole budget, and GPU scheduling is explicitly out of scope (GPU-aware routing is telemetry-only, ADR-0006). |

## Cross-section assumptions

- The 50 ms p95 assembly SLO and the 5k–10k req/s / ~0.7 vCPU-per-1k envelope are the canonical figures (Section 9 owns the latency table); this ADR splits the 50 ms into a 35 ms internal budget + 15 ms headroom but never restates a *different* SLO.
- The `weights` vector is the single weight vocabulary owned by the Context Assembler (ADR-0005); the kernel consumes it, it does not define a second one.
- `token_budget` arrives already net of the hard reserve because the router selected the model (and thus the tokenizer) before final packing (consistency rule C3); the kernel never re-derives the reserve.
