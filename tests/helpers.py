"""Importable test helpers (on the pytest pythonpath)."""

from __future__ import annotations

from contextos.models.common import MemoryTier, Provenance
from contextos.models.memory import MemoryObject
from contextos.security.context import SecurityContext


def make_ctx(tenant: str, user: str, namespace: str | None = None) -> SecurityContext:
    return SecurityContext.resolve(tenant_id=tenant, user_id=user, namespace=namespace)


def make_memory(ctx: SecurityContext, content: str) -> MemoryObject:
    return MemoryObject(
        tenant_id=ctx.tenant_id,
        namespace=ctx.namespace,
        tier=MemoryTier.LONG_TERM,
        content=content,
        provenance=Provenance(source="test"),
    )
