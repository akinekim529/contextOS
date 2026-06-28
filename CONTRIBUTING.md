# Contributing to ContextOS

Thanks for helping build the context layer every LLM platform otherwise reinvents.

## Ground rules that are non-negotiable

ContextOS is security-sensitive multi-tenant middleware. Two invariants gate every PR:

1. **Zero cross-tenant leakage.** No memory, cache entry, context block, or trace ever crosses a tenant boundary. The `tests/leakage/` property suite fires ≥10k hostile second-tenant probes and is a **hard CI gate**. If your change touches a data path, it must keep that suite green.
2. **Stay inside the scope boundary.** ContextOS uses a vector DB, an inference engine, and an embedder — it never reimplements them. In-process scoring runs over **pre-retrieved candidates only (≤512)**; we never build or own an index. See [ADR-0006](docs/adr/) and the architecture scope section.

## Dev setup

```bash
uv sync --extra dev          # create the env from the locked manifest
uv run ruff check .          # lint
uv run mypy                  # strict type-check
uv run pytest -m "not integration"   # unit + leakage (no containers needed)
uv run pytest -m integration         # spins Postgres/Redis via testcontainers
```

## Design decisions

Significant choices live as ADRs under [`docs/adr/`](docs/adr/), one file per decision, each naming the rejected alternative and why it fails. If your PR changes a decision recorded there, supersede the ADR rather than editing history.

## PR checklist

- [ ] `ruff`, `mypy`, and `pytest` pass locally.
- [ ] New data paths covered by a leakage test; new context decisions covered by a replay test.
- [ ] No new placeholder text (`TODO`, `etc.`, `to be defined`) in design docs.
- [ ] Any new component is mapped to a roadmap milestone.
- [ ] Conventional-commit title (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
