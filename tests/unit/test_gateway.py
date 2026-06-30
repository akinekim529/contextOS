"""Gateway happy path + fail-closed auth, using the fake adapter (no network/GPU)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from contextos.adapters.base import (
    Capabilities,
    ChatRequest,
    ChatResponse,
    StreamEvent,
    StreamEventType,
    Usage,
)
from contextos.adapters.fake import FakeAdapter
from contextos.gateway.app import create_app


class _EchoAdapter:
    """Returns the concatenated prompt so a test can prove memory was injected into it."""

    name = "echo"

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def health_check(self) -> bool:
        return True

    async def generate(self, req: ChatRequest) -> ChatResponse:
        text = " | ".join(m.content for m in req.messages)
        return ChatResponse(text=text, model=req.model, finish_reason="stop", usage=Usage())

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


def _client() -> TestClient:
    return TestClient(create_app(adapter=FakeAdapter("hello from contextos")))


def test_chat_returns_text_and_trace() -> None:
    client = _client()
    r = client.post(
        "/v1/chat",
        json={"prompt": "how do I deploy an LLM on Kubernetes?"},
        headers={"X-Tenant-Id": "acme", "X-User-Id": "123"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "hello from contextos"
    assert body["trace_id"]

    # The trace stub is readable and carries one span per pipeline stage (seed of replay).
    tr = client.get(f"/v1/traces/{body['trace_id']}", headers={"X-Tenant-Id": "acme", "X-User-Id": "123"})
    assert tr.status_code == 200, tr.text
    stages = {s["stage"] for s in tr.json()["spans"]}
    assert {"auth", "route", "backend.invoke", "writeback"} <= stages


def test_missing_tenant_is_denied() -> None:
    client = _client()
    r = client.post("/v1/chat", json={"prompt": "hi"}, headers={"X-User-Id": "123"})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "access_denied"


def test_trace_is_tenant_scoped() -> None:
    client = _client()
    r = client.post("/v1/chat", json={"prompt": "hi"}, headers={"X-Tenant-Id": "acme", "X-User-Id": "1"})
    trace_id = r.json()["trace_id"]
    # A different tenant cannot read acme's trace.
    other = client.get(f"/v1/traces/{trace_id}", headers={"X-Tenant-Id": "evil", "X-User-Id": "9"})
    assert other.status_code == 404


def test_memory_write_then_chat_records_retrieval() -> None:
    client = _client()
    h = {"X-Tenant-Id": "acme", "X-User-Id": "123"}

    w = client.post("/v1/memory", json={"content": "user's prod region is eu-west-1"}, headers=h)
    assert w.status_code == 200, w.text

    r = client.post("/v1/chat", json={"prompt": "which region is prod deployed in?"}, headers=h)
    trace = client.get(f"/v1/traces/{r.json()['trace_id']}", headers=h).json()
    retrieve = next(s for s in trace["spans"] if s["stage"] == "retrieve")
    assert int(retrieve["decision"]["pointers"]["candidates"]) >= 1


def test_assembler_injects_remembered_fact_into_prompt() -> None:
    client = TestClient(create_app(adapter=_EchoAdapter()))
    h = {"X-Tenant-Id": "acme", "X-User-Id": "123"}
    client.post("/v1/memory", json={"content": "user's prod region is eu-west-1"}, headers=h)
    r = client.post("/v1/chat", json={"prompt": "which region is prod deployed in?"}, headers=h)
    assert r.status_code == 200, r.text
    # The echo adapter returns the assembled prompt — the remembered fact must be in it.
    assert "eu-west-1" in r.json()["text"]
    # And the assemble span recorded the injection.
    trace = client.get(f"/v1/traces/{r.json()['trace_id']}", headers=h).json()
    assemble = next(s for s in trace["spans"] if s["stage"] == "assemble")
    assert int(assemble["decision"]["pointers"]["injected"]) >= 1


def test_identical_query_is_served_from_cache() -> None:
    client = _client()  # FakeAdapter; user has no memories -> response is cacheable
    h = {"X-Tenant-Id": "acme", "X-User-Id": "777"}
    body = {"prompt": "what is kubernetes?"}
    r1 = client.post("/v1/chat", json=body, headers=h)
    r2 = client.post("/v1/chat", json=body, headers=h)
    assert r1.status_code == r2.status_code == 200

    def cache_span(trace_id: str) -> dict:
        spans = client.get(f"/v1/traces/{trace_id}", headers=h).json()["spans"]
        return next(s for s in spans if s["stage"] == "cache")["decision"]["pointers"]

    assert cache_span(r1.json()["trace_id"])["verdict"] == "miss"   # cold
    hit = cache_span(r2.json()["trace_id"])
    assert hit["verdict"] == "hit" and hit["tier"] == "exact"       # warm -> exact hit


def test_replay_endpoint_reproduces_decision() -> None:
    client = TestClient(create_app(adapter=_EchoAdapter()))
    h = {"X-Tenant-Id": "acme", "X-User-Id": "123"}
    client.post("/v1/memory", json={"content": "user's prod region is eu-west-1"}, headers=h)
    tid = client.post("/v1/chat", json={"prompt": "which region is prod in?"}, headers=h).json()["trace_id"]

    rep = client.get(f"/v1/traces/{tid}/replay", headers=h)
    assert rep.status_code == 200, rep.text
    body = rep.json()
    assert body["prompt_equal"] is True          # the assembled prompt reproduces bit-for-bit
    assert body["output_equal"] is True
    assert body["bundle_cid"].startswith("b2:")
    assert "eu-west-1" in (body.get("recorded_output") or "")

    bundle = client.get(f"/v1/traces/{tid}/bundle", headers=h).json()
    assert any("eu-west-1" in c["content"] for c in bundle["candidates"])

    # Another tenant cannot replay acme's decision.
    other = client.get(f"/v1/traces/{tid}/replay", headers={"X-Tenant-Id": "evil", "X-User-Id": "9"})
    assert other.status_code == 404


def test_hard_reserve_overflow_returns_413() -> None:
    from contextos.config.settings import ContextOSSettings

    # Window smaller than the output reservation -> hard reserves cannot fit -> fail closed.
    app = create_app(settings=ContextOSSettings(default_token_budget=50), adapter=FakeAdapter())
    client = TestClient(app)
    r = client.post(
        "/v1/chat",
        json={"prompt": "a question", "max_tokens": 256},
        headers={"X-Tenant-Id": "acme", "X-User-Id": "1"},
    )
    assert r.status_code == 413
    assert r.json()["error"]["type"] == "context_overflow"


def test_admin_cost_and_memory_versioning_endpoints() -> None:
    client = TestClient(create_app(adapter=FakeAdapter("ok")))
    h = {"X-Tenant-Id": "acme", "X-User-Id": "42"}
    client.post("/v1/chat", json={"prompt": "hello world"}, headers=h)
    cost = client.get("/v1/admin/cost", headers=h).json()
    assert cost["tenant_id"] == "acme" and cost["requests"] >= 1

    client.post("/v1/memory", json={"content": "fact A"}, headers=h)
    c1 = client.post("/v1/admin/memory/commit", json={"label": "first"}, headers=h).json()["cid"]
    client.post("/v1/memory", json={"content": "fact B"}, headers=h)
    c2 = client.post("/v1/admin/memory/commit", json={}, headers=h).json()["cid"]
    diff = client.get(f"/v1/admin/memory/diff?a={c1}&b={c2}", headers=h).json()
    assert len(diff["added"]) == 1


def test_otel_export_endpoint() -> None:
    client = TestClient(create_app(adapter=FakeAdapter("ok")))
    h = {"X-Tenant-Id": "acme", "X-User-Id": "1"}
    tid = client.post("/v1/chat", json={"prompt": "hi"}, headers=h).json()["trace_id"]
    otel = client.get(f"/v1/traces/{tid}/otel", headers=h).json()
    assert otel["resourceSpans"][0]["scopeSpans"][0]["spans"]

