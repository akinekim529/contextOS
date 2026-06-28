"""In-memory StorageBackend.

This is the reference implementation of the repository-boundary firewall and the target of
the tenant-isolation property suite (``tests/leakage/``). It enforces, in code, exactly what
Postgres FORCE RLS enforces in the database: a read is filtered to ``ctx.tenant_id`` AND
``ctx.namespace``, and a write that tries to escape the caller's scope is denied.

If this implementation can be made to leak across tenants, so can a missing ``WHERE`` clause —
which is the whole point of testing it with tens of thousands of hostile probes.
"""

from __future__ import annotations

from ..models.memory import MemoryObject
from ..security.context import SecurityContext
from ..security.errors import AccessDenied


class InMemoryStore:
    def __init__(self) -> None:
        # Physically partition by tenant so a lookup can never even *see* another tenant's dict.
        self._by_tenant: dict[str, dict[str, MemoryObject]] = {}

    async def add_memory(self, ctx: SecurityContext, memory: MemoryObject) -> MemoryObject:
        # A principal may only write within its own scope. We do not trust the object's fields;
        # we assert they match the context and refuse otherwise (no silent rewrite either).
        if memory.tenant_id != ctx.tenant_id:
            raise AccessDenied("write tenant != context tenant", principal=ctx.principal.id)
        if memory.namespace != ctx.namespace:
            raise AccessDenied("write namespace != context namespace", principal=ctx.principal.id)
        self._by_tenant.setdefault(ctx.tenant_id, {})[memory.id] = memory
        return memory

    async def get_memory(self, ctx: SecurityContext, memory_id: str) -> MemoryObject | None:
        row = self._by_tenant.get(ctx.tenant_id, {}).get(memory_id)
        if row is None:
            return None
        # Namespace is a hard filter (C2): a row in another namespace is invisible.
        if row.namespace != ctx.namespace:
            return None
        return row

    async def list_memories(self, ctx: SecurityContext, *, limit: int = 100) -> list[MemoryObject]:
        rows = self._by_tenant.get(ctx.tenant_id, {}).values()
        scoped = [r for r in rows if r.namespace == ctx.namespace]
        return scoped[:limit]

    async def health_check(self) -> bool:
        return True
