"""Git-like memory versioning over the Memory Engine.

``commit`` snapshots the in-scope memory state under a content id; ``branch`` forks a head;
``diff`` reports added/removed/changed memories between two commits; ``rollback`` restores a
commit's memories that are currently missing. Snapshots are tenant-partitioned, so one tenant
can never see or restore another's history.

v1 rollback is append-only (it restores missing memories; it does not delete newer ones) — a
real, documented semantics. Hard delete lands with the store delete path (RTBF, deferred).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..memory.engine import MemoryEngine
from ..models.common import new_ulid, to_rfc3339, utcnow
from ..models.memory import MemoryObject
from ..security.context import SecurityContext


@dataclass(frozen=True)
class Commit:
    cid: str
    branch: str
    label: str
    created_at: str
    snapshot: tuple[MemoryObject, ...]


@dataclass(frozen=True)
class MemoryDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


class UnknownCommit(KeyError):
    """The commit id does not exist for this tenant."""


class MemoryVersioning:
    def __init__(self, engine: MemoryEngine) -> None:
        self._engine = engine
        self._commits: dict[str, dict[str, Commit]] = {}      # tenant -> cid -> Commit
        self._branches: dict[str, dict[str, str | None]] = {}  # tenant -> branch -> head cid

    async def commit(self, ctx: SecurityContext, label: str = "", *, branch: str = "main") -> str:
        rows = await self._engine.all_memories(ctx, limit=4096)
        cid = new_ulid()
        commit = Commit(
            cid=cid, branch=branch, label=label, created_at=to_rfc3339(utcnow()),
            snapshot=tuple(r.model_copy(deep=True) for r in rows),
        )
        self._commits.setdefault(ctx.tenant_id, {})[cid] = commit
        self._branches.setdefault(ctx.tenant_id, {})[branch] = cid
        return cid

    def branch(self, ctx: SecurityContext, name: str, *, from_branch: str = "main") -> None:
        heads = self._branches.setdefault(ctx.tenant_id, {})
        heads[name] = heads.get(from_branch)

    def branches(self, ctx: SecurityContext) -> dict[str, str | None]:
        return dict(self._branches.get(ctx.tenant_id, {}))

    def get(self, ctx: SecurityContext, cid: str) -> Commit | None:
        return self._commits.get(ctx.tenant_id, {}).get(cid)  # tenant-scoped

    def diff(self, ctx: SecurityContext, a_cid: str, b_cid: str) -> MemoryDiff:
        a, b = self.get(ctx, a_cid), self.get(ctx, b_cid)
        if a is None or b is None:
            raise UnknownCommit("unknown commit for this tenant")
        a_map = {m.id: m.content for m in a.snapshot}
        b_map = {m.id: m.content for m in b.snapshot}
        return MemoryDiff(
            added=sorted(i for i in b_map if i not in a_map),
            removed=sorted(i for i in a_map if i not in b_map),
            changed=sorted(i for i in a_map if i in b_map and a_map[i] != b_map[i]),
        )

    async def rollback(self, ctx: SecurityContext, cid: str) -> int:
        commit = self.get(ctx, cid)
        if commit is None:
            raise UnknownCommit("unknown commit for this tenant")
        present = {m.id for m in await self._engine.all_memories(ctx, limit=4096)}
        restored = 0
        for m in commit.snapshot:
            if m.id not in present:
                await self._engine.put(ctx, m.model_copy(deep=True))
                restored += 1
        return restored
