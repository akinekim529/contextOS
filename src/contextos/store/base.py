"""Storage backend protocol. Every method takes a SecurityContext and scopes by it.

There is deliberately no "get everything" method and no way to pass a raw tenant_id string
— the only way to read is through a SecurityContext, so a missing scope is a type error,
not a silent full-table scan.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models.memory import MemoryObject
from ..security.context import SecurityContext


@runtime_checkable
class StorageBackend(Protocol):
    async def add_memory(self, ctx: SecurityContext, memory: MemoryObject) -> MemoryObject: ...

    async def get_memory(self, ctx: SecurityContext, memory_id: str) -> MemoryObject | None: ...

    async def list_memories(self, ctx: SecurityContext, *, limit: int = 100) -> list[MemoryObject]: ...

    async def health_check(self) -> bool: ...
