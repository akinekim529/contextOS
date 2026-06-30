# Launch playbook

The goal of launch is one undeniable demo + a frictionless quickstart, in front of the right
audience, at the right time. Everything below serves that.

## Pre-flight checklist (do not launch until all are ✅)

- [ ] `pytest -m "not integration"` green; `ruff` + `mypy --strict` clean; CI badge green on `main`.
- [ ] `python examples/quickstart.py` and `examples/replay_demo.py` run clean on a fresh clone.
- [ ] README hero leads with the wedge + the real replay output (already captured).
- [ ] A **GIF / asciinema** of `replay_demo.py` (record it; embed at the top of the README).
- [ ] [docs/comparison.md](docs/comparison.md) table is honest and current.
- [ ] `STATUS.md` clearly separates "runs today" from "designed/deferred" (no "production-ready" overclaim).
- [ ] Repo: public, Apache-2.0, a crisp **About** description, topics set, issue templates live on the default branch.
- [ ] 6–10 `good first issue`-labelled issues filed (see [docs/good-first-issues.md](docs/good-first-issues.md)).
- [ ] `SECURITY.md` + private vulnerability reporting enabled.

## The one-liner

> **ContextOS — see (and replay) exactly why your LLM said that.** Middleware that turns the
> context window into a per-tenant, budget-constrained, fully-replayable decision in front of any
> backend.

## Launch sequence (one week)

**Day 0 (Tue–Thu, ~8–10am ET) — Show HN.** This is the main event.
- Title: `Show HN: ContextOS – see (and replay) exactly why your LLM said that`
- First comment: the problem (RAG can't tell you *why*), the wedge, the 5-minute quickstart, and
  what's real vs deferred. Be present for the first 2–3 hours; answer every comment fast and plainly.

**Day 0–1 — Reddit, with the GIF (not just a link).**
- r/LocalLLaMA: framed around vLLM/Ollama users + cost routing + offline demo.
- r/MachineLearning ([P]) and r/devops: framed around multi-tenant isolation + replay/observability.

**Day 1 — Lobste.rs, and the launch blog post.**
- Blog title: *"Your RAG can't tell you why — ContextOS can."* Walk through one replayed decision.
- Cross-post to dev.to / Hashnode.

**Day 1–2 — Communities & X/Twitter.**
- vLLM, LangChain, LlamaIndex Discords/Slacks (share, don't spam; lead with the demo).
- X thread: GIF + 3–4 posts (problem → wedge → replay → quickstart), tag relevant infra folks.

## After launch (sustain)

- Reply to every issue/PR within a day for the first two weeks. The templates are already set up.
- Ship small, frequent releases with a changelog. Tag `0.1.x`.
- Land an integration (LangChain/LlamaIndex adapter) to ride an existing audience.
- Put numbers in front: `0` cross-tenant leaks (≥10k probes), token savings, replay bit-exactness.
- Stand up a docs site from `docs/` (mkdocs-material) once traffic justifies it.

## Honest framing

Lead with "early but real + a killer idea + a working demo," and invite contributors. Never imply
the production infra is deployed — credibility is the scarcest resource at launch.
