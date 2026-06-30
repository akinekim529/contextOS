"""Memory consolidation — the design's cross-session summarization, as a real batch job.

Clusters a namespace's memories and writes one consolidated SEMANTIC memory via the backend
adapter. It is an async, rate-limitable, cost-trackable job (NOT an agent loop): it never
schedules or re-executes anything; it summarizes and writes once. Run it off the hot path
through the :class:`BackgroundRunner`.
"""

from __future__ import annotations

from ..adapters.base import BackendAdapter, ChatMessage, ChatRequest, Role
from ..memory.engine import MemoryEngine
from ..models.common import MemoryTier
from ..security.context import SecurityContext


async def consolidate_namespace(
    engine: MemoryEngine,
    llm: BackendAdapter,
    ctx: SecurityContext,
    *,
    min_cluster: int = 2,
) -> int:
    """Summarize in-scope memories into one consolidated fact. Returns # of sources consolidated."""
    rows = await engine.all_memories(ctx, limit=512)
    if len(rows) < min_cluster:
        return 0
    joined = "\n".join(f"- {r.content}" for r in rows)
    prompt = (
        "Consolidate these memories into a few durable facts. Preserve every number, identifier, "
        f"and named entity verbatim; do not invent anything.\n{joined}"
    )
    resp = await llm.generate(
        ChatRequest(
            model="consolidation",
            messages=[ChatMessage(role=Role.USER, content=prompt)],
            max_tokens=256,
        )
    )
    await engine.write(
        ctx, resp.text.strip(), tier=MemoryTier.SEMANTIC, importance=0.7, source="consolidation"
    )
    return len(rows)
