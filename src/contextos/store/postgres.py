"""Postgres-backed StorageBackend with Row-Level Security as the isolation backstop.

Defense-in-depth (ADR-0002): the app firewall scopes every query, and Postgres FORCE RLS
makes a missing scope harmless — the policy ``USING (tenant_id = current_setting('app.tenant_id'))``
filters rows even if application code forgets a predicate. We set the GUC with ``SET LOCAL`` so
it is scoped to the transaction and cannot bleed across pooled connections.

``asyncpg`` is imported lazily so the package (and the leakage property tests) install without
a database driver. This backend is exercised by ``tests/integration/`` against a real Postgres.
"""

from __future__ import annotations

import json
from typing import Any

from ..models.common import EmbeddingRef, MemoryTier, Provenance
from ..models.memory import MemoryObject
from ..security.context import SecurityContext


async def _register_jsonb_codec(conn: Any) -> None:  # pragma: no cover - integration only
    # asyncpg has no built-in dict<->jsonb codec; register one so we pass/return plain dicts.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class PostgresStore:  # pragma: no cover - exercised only by the integration suite (needs a DB)
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any | None = None

    async def connect(self) -> None:
        import asyncpg  # lazy: optional dependency

        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=10, init=_register_jsonb_codec
        )

    async def _scoped_conn(self, ctx: SecurityContext) -> tuple[Any, Any]:
        if self._pool is None:
            raise RuntimeError("call connect() first")
        conn = await self._pool.acquire()
        tx = conn.transaction()
        await tx.start()
        # Bind the RLS GUC for the lifetime of this transaction only.
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", ctx.tenant_id)
        await conn.execute("SELECT set_config('app.namespace', $1, true)", ctx.namespace)
        return conn, tx

    async def add_memory(self, ctx: SecurityContext, memory: MemoryObject) -> MemoryObject:
        conn, tx = await self._scoped_conn(ctx)
        try:
            await conn.execute(
                """
                INSERT INTO memories (id, tenant_id, namespace, tier, content, importance,
                                      created_at, last_accessed_at, provenance, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                memory.id, memory.tenant_id, memory.namespace, memory.tier.value, memory.content,
                memory.importance, memory.created_at, memory.last_accessed_at,
                memory.provenance.model_dump(mode="json"), memory.metadata,
            )
            await tx.commit()
            return memory
        except Exception:
            await tx.rollback()
            raise
        finally:
            await self._pool.release(conn)  # type: ignore[union-attr]

    async def list_memories(self, ctx: SecurityContext, *, limit: int = 100) -> list[MemoryObject]:
        conn, tx = await self._scoped_conn(ctx)
        try:
            # No tenant_id predicate here ON PURPOSE — RLS supplies it. The integration test
            # asserts this query cannot return another tenant's rows even though we "forgot".
            rows = await conn.fetch("SELECT * FROM memories ORDER BY last_accessed_at DESC LIMIT $1", limit)
            await tx.commit()
            return [self._row_to_memory(r) for r in rows]
        except Exception:
            await tx.rollback()
            raise
        finally:
            await self._pool.release(conn)  # type: ignore[union-attr]

    async def get_memory(self, ctx: SecurityContext, memory_id: str) -> MemoryObject | None:
        conn, tx = await self._scoped_conn(ctx)
        try:
            row = await conn.fetchrow("SELECT * FROM memories WHERE id = $1", memory_id)
            await tx.commit()
            return self._row_to_memory(row) if row else None
        finally:
            await self._pool.release(conn)  # type: ignore[union-attr]

    @staticmethod
    def _row_to_memory(row: Any) -> MemoryObject:
        return MemoryObject(
            id=row["id"], tenant_id=row["tenant_id"], namespace=row["namespace"],
            tier=MemoryTier(row["tier"]), content=row["content"], importance=row["importance"],
            created_at=row["created_at"], last_accessed_at=row["last_accessed_at"],
            provenance=Provenance.model_validate(row["provenance"]),
            embedding_ref=(
                EmbeddingRef(collection="memories", vector_id=row["id"]) if row.get("embedding_ref") else None
            ),
            metadata=dict(row["metadata"]) if row["metadata"] else {},
        )

    async def health_check(self) -> bool:
        if self._pool is None:
            return False
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT 1"))
