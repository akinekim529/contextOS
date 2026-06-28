"""Real-Postgres RLS backstop test (the DB half of defense-in-depth).

Marked ``integration`` — skipped unless a Postgres is reachable (CI spins one via
testcontainers). It deliberately issues a query with NO tenant predicate and asserts FORCE RLS
still prevents cross-tenant rows: the application could forget the ``WHERE`` and the database
would still hold the line.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.integration]

DSN = os.environ.get("CONTEXTOS_POSTGRES_DSN")


@pytest.mark.skipif(not DSN, reason="set CONTEXTOS_POSTGRES_DSN to run the RLS integration test")
@pytest.mark.asyncio
async def test_force_rls_blocks_cross_tenant_select() -> None:
    from contextos.store.postgres import PostgresStore
    from helpers import make_ctx, make_memory

    store = PostgresStore(DSN)  # type: ignore[arg-type]
    await store.connect()

    a = make_ctx("tenant-a", "ua", "alpha")
    b = make_ctx("tenant-b", "ub", "alpha")
    await store.add_memory(a, make_memory(a, "tenant-a secret"))
    await store.add_memory(b, make_memory(b, "tenant-b secret"))

    # Listing under tenant-a's GUC must never surface tenant-b's row, even though
    # list_memories() issues `SELECT * FROM memories` with no tenant predicate.
    rows = await store.list_memories(a, limit=1000)
    assert rows, "expected tenant-a's own row"
    assert all(r.tenant_id == "tenant-a" for r in rows)
