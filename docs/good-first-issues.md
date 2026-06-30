# Good first issues

Concrete, well-scoped tasks for new contributors. Each is self-contained, has a clear
acceptance test, and follows the existing module patterns. File these as GitHub issues with the
`good first issue` label at launch.

| # | Task | Where | Acceptance |
|---|---|---|---|
| 1 | **Live Ollama smoke test** behind `@pytest.mark.integration` | `tests/integration/` | Spins/uses a local Ollama, runs `ollama_adapter` `generate`; skipped without a server |
| 2 | **Qdrant `VectorStore` adapter** | `src/contextos/store/` | Implements the store protocol against Qdrant; passes the leakage suite |
| 3 | **tiktoken `Tokenizer`** to replace the heuristic | `src/contextos/assembler/tokenizer.py` | Exact counts for a known model; falls back to heuristic if tiktoken absent |
| 4 | **Distilled difficulty classifier** (stage-two routing) | `src/contextos/router/difficulty.py` | Optional model-based estimate; heuristic stays the default |
| 5 | **Cross-encoder reranker** (opt-in, out-of-band) | `src/contextos/assembler/` | Re-scores ≤512 candidates; off by default; documented scope boundary |
| 6 | **Abstractive-compression eval harness** | `tests/` + `docs/` | Measures fact-retention vs ratio on a small fixture set |
| 7 | **Redis-backed cache + worker plane** | `src/contextos/cache/`, `workers/` | Implements the backends behind the existing interfaces; integration-gated |
| 8 | **SSE streaming end-to-end** through the gateway | `src/contextos/gateway/` | `POST /v1/chat` with `Accept: text/event-stream`; write-back tees on terminal event |
| 9 | **mkdocs-material docs site** from `docs/` | `mkdocs.yml` | `mkdocs build` renders the design docs + ADRs |
| 10 | **`langchain` integration adapter** | `integrations/` | A LangChain LLM/Retriever that routes through ContextOS |

Pick one, comment on the issue to claim it, and read [CONTRIBUTING.md](../CONTRIBUTING.md) for the
two non-negotiables (zero cross-tenant leakage; stay inside the scope boundary).
