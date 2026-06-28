"""Gateway happy path + fail-closed auth, using the fake adapter (no network/GPU)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from contextos.adapters.fake import FakeAdapter
from contextos.gateway.app import create_app


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

