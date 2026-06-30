# Status — ContextOS v0.1.0

Honest maturity per module. **Runs** = real, in-memory, dependency-free, unit-tested, and green
under `ruff` + `mypy --strict` + `pytest`. **Production path** = the documented backend that
replaces the in-memory reference *behind the same interface* for real deployments.

## Implemented and tested — runs today, no infrastructure

| Module | What runs | Production path (designed) |
|---|---|---|
| Memory Engine | four tiers, hybrid RRF retrieval, recency/importance rescore, MMR, decay | Postgres + pgvector |
| Context Assembler | blended ranking, token-budget packing (413 fail-closed), MMR, edge-loading | pure CPU; optional Rust kernel |
| Context Compressor | structural + extractive + abstractive (via adapter) + fact-retention guard | — |
| Semantic Cache | two-tier exact+ANN, per-tenant, TTL, fail-open | Redis + pgvector |
| Model Router | cost/quality/latency utility, difficulty, circuit breakers, fallback chains | — |
| RBAC firewall | SecurityContext, deny-overrides policy engine, namespace hard-filter | + Postgres FORCE RLS |
| Replay Debugger (flagship) | content-addressed bundle, bit-exact replay, context diff | DEK-sealed storage, two-phase write |
| Memory versioning | commit / branch / diff / rollback | — |
| Workers | in-process async runner + memory consolidation | Redis Streams |
| Observability | spans, decision records, cost ledger, OTLP export | OTel collector, fail-closed cost outbox |
| Adapters | vLLM, OpenAI-compatible, Ollama, TGI, fake (wire-tested via MockTransport) | — |
| Gateway + SDK | FastAPI `/v1/*` + the 2-line in-process client | — |
| Tenant isolation | ≥10k hostile-probe property gate (CI hard gate) | + real-Postgres RLS integration test |

## Designed, deferred — in `docs/design/`, **not** shipped as fake code

These require infrastructure and are honestly deferred:

- Kubernetes HA across AZs — the Helm chart + Dockerfile are the real scaffold.
- KMS envelope encryption + crypto-shred right-to-be-forgotten.
- ML-based prompt-injection defense (the tri-layer design).
- The optional Rust/PyO3 hot-path kernel — gated by [ADR-0001](docs/adr/0001-rust-hotpath-gate.md).
- Real vLLM / Postgres / Redis / Qdrant at scale; learned/bandit routing; multi-region.

See [the roadmap](docs/design/09-roadmap.md) for sequencing.

## Quality gates (enforced every commit)

`ruff check src tests` · `mypy --strict` · `pytest -m "not integration"` (incl. the leakage gate)
· a no-placeholder grep · the examples run offline. The Postgres RLS backstop runs as a
non-blocking CI job until verified in your environment.
