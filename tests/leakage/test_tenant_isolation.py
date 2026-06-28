"""Tenant-isolation property suite — the CI hard gate.

Fires >= 10,000 hostile probes at the repository boundary and asserts zero cross-tenant and
cross-namespace leakage. If this implementation can be made to leak, a forgotten ``WHERE``
clause can too — which is exactly what we want this test to catch before it ships.

Two complementary strategies:
  * an explicit, seeded 10k-probe sweep (deterministic, fast, satisfies the >=10k gate), and
  * a Hypothesis property test (shrinks any counterexample to a minimal repro).
"""

from __future__ import annotations

import asyncio
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from contextos.models.common import Action
from contextos.security.errors import AccessDenied, MissingNamespace, MissingTenant
from contextos.security.rbac import PolicyEngine
from contextos.store.memory_store import InMemoryStore
from helpers import make_ctx, make_memory

pytestmark = pytest.mark.leakage

PROBES = 10_000
TENANTS = [f"tenant-{i}" for i in range(12)]
NAMESPACES = ["alpha", "beta", "gamma"]


async def _populate() -> tuple[InMemoryStore, dict[str, list[str]]]:
    store = InMemoryStore()
    ids_by_tenant: dict[str, list[str]] = {t: [] for t in TENANTS}
    for t in TENANTS:
        for ns in NAMESPACES:
            ctx = make_ctx(t, f"user-{t}", ns)
            for k in range(5):
                mem = make_memory(ctx, f"secret of {t}/{ns} #{k}")
                await store.add_memory(ctx, mem)
                ids_by_tenant[t].append(mem.id)
    return store, ids_by_tenant


@pytest.mark.asyncio
async def test_no_cross_tenant_or_namespace_leak_under_10k_probes() -> None:
    rng = random.Random(1337)  # seeded -> deterministic CI
    store, ids_by_tenant = await _populate()

    for _ in range(PROBES):
        tenant = rng.choice(TENANTS)
        namespace = rng.choice(NAMESPACES)
        probe = make_ctx(tenant, f"attacker-{tenant}", namespace)

        # 1. A scoped list NEVER returns another tenant's or another namespace's row.
        for row in await store.list_memories(probe, limit=1000):
            assert row.tenant_id == tenant, "cross-tenant leak in list_memories"
            assert row.namespace == namespace, "cross-namespace leak in list_memories"

        # 2. Fetching a known id that belongs to a *different* tenant resolves to None.
        foreign = rng.choice([t for t in TENANTS if t != tenant])
        foreign_id = rng.choice(ids_by_tenant[foreign])
        assert await store.get_memory(probe, foreign_id) is None, "cross-tenant get leaked a row"


def test_policy_engine_denies_cross_tenant_and_cross_namespace() -> None:
    engine = PolicyEngine()
    ctx = make_ctx("tenant-a", "u1", "alpha")

    # cross-tenant resource -> deny, before any rule
    with pytest.raises(AccessDenied):
        engine.check(ctx, resource={"type": "memory", "tenant_id": "tenant-b", "namespace": "alpha"},
                     action=Action.READ)

    # cross-namespace within tenant -> deny under the default-deny floor (C2)
    with pytest.raises(AccessDenied):
        engine.check(ctx, resource={"type": "memory", "tenant_id": "tenant-a", "namespace": "beta"},
                     action=Action.READ)

    # same tenant + namespace -> allowed
    engine.check(ctx, resource={"type": "memory", "tenant_id": "tenant-a", "namespace": "alpha"},
                 action=Action.READ)


def test_missing_tenant_or_namespace_fails_closed() -> None:
    with pytest.raises(MissingTenant):
        make_ctx("", "u1", "alpha")
    with pytest.raises(MissingNamespace):
        # no namespace and no user to derive one from
        from contextos.security.context import SecurityContext

        SecurityContext.resolve(tenant_id="tenant-a", user_id=None, namespace=None)


@settings(max_examples=300, deadline=None)
@given(
    rows=st.lists(
        st.tuples(st.sampled_from(TENANTS), st.sampled_from(NAMESPACES), st.text(min_size=1, max_size=20)),
        min_size=1,
        max_size=60,
    ),
    probe=st.tuples(st.sampled_from(TENANTS), st.sampled_from(NAMESPACES)),
)
def test_property_scoped_reads_are_pure(rows: list[tuple[str, str, str]], probe: tuple[str, str]) -> None:
    async def _run() -> None:
        store = InMemoryStore()
        for t, ns, content in rows:
            ctx = make_ctx(t, f"user-{t}", ns)
            await store.add_memory(ctx, make_memory(ctx, content))
        p_tenant, p_ns = probe
        pctx = make_ctx(p_tenant, f"attacker-{p_tenant}", p_ns)
        for row in await store.list_memories(pctx, limit=1000):
            assert row.tenant_id == p_tenant and row.namespace == p_ns

    asyncio.run(_run())
