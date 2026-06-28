# ContextOS Design Document

The full, implementation-first design. Read in order, or jump to a module.

| # | Section | What it covers |
|---|---|---|
| 0 | [Executive Summary](00-executive-summary.md) | Thesis, the wedge, flagship, headline numbers |
| 1 | [System Architecture](01-system-architecture.md) | Component diagram, data flow, hot path vs async plane, consistency model |
| 2 | Module Deep Dive | The seven core modules: |
| 2.1 | [Memory Engine](02-module-deep-dive/2.1-memory-engine.md) | Four tiers, hybrid RRF retrieval, decay, consolidation |
| 2.2 | [Context Assembler](02-module-deep-dive/2.2-context-assembler.md) | Scoring, token-budget packing, MMR, edge-loading |
| 2.3 | [RBAC Context Firewall](02-module-deep-dive/2.3-rbac-firewall.md) | Row-level isolation, deny-overrides, four enforcement points |
| 2.4 | [Semantic Cache](02-module-deep-dive/2.4-semantic-cache.md) | Two-tier exact + ANN, coarse fingerprint, per-tenant |
| 2.5 | [Context Compressor](02-module-deep-dive/2.5-context-compressor.md) | Structural/extractive/abstractive, fact-retention guard |
| 2.6 | [Model Router](02-module-deep-dive/2.6-model-router.md) | Utility scoring, difficulty, breakers, fallback chains |
| 2.7 | [Observability](02-module-deep-dive/2.7-observability.md) | Spans, decision records, replay manifests |
| 3 | [API Design](03-api-design.md) | Python SDK (simple + power), REST, streaming, admin |
| 4 | [Data Models](04-data-models.md) | The five typed schemas with example payloads |
| 5 | [Deployment Design](05-deployment-design.md) | Helm, microservices, scaling, backing stores, vLLM/GPU |
| 6 | [Killer Features](06-killer-features.md) | Flagship Replay Debugger + 5 supporting features |
| 7 | [Security Model](07-security-model.md) | Isolation, prompt-injection, secrets, GDPR/KVKK |
| 8 | [Non-Functional Requirements](08-nfr.md) | Authoritative latency budget, throughput, HA, cost |
| 9 | [Roadmap](09-roadmap.md) | Week 1–2 / Month 1 / Month 3 with deferrals |
| 10 | [GitHub Strategy](10-github-strategy.md) | Launch plan to the first 1k stars |

**Decisions** are recorded as ADRs in [`../adr/`](../adr/). The hot-path component diagram is at [`../diagrams/component-hotpath.txt`](../diagrams/component-hotpath.txt).

## Canonical numbers (single source of truth — [§8](08-nfr.md) owns the budget table)

`< 50 ms p95` context assembly · `< 100 ms p95` memory retrieval · `< 250 ms p95` control overhead · pgvector ANN p95 `18 ms` · `5k–10k req/s`/node · `99.9%` gateway availability · `0` cross-tenant leaks (`≥10k` hostile-probe CI gate) · cache hit-ratio `25–45%` · token-cost savings `40–65%`.
