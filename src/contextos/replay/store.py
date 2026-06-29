"""Bundle store. Tenant-partitioned, so a replay can never read across the tenant boundary.

The reference store keeps bundles in memory keyed by trace_id. Production is the two-phase write
from the design: a synchronous pointer stub on the hot path + asynchronous, DEK-sealed,
content-addressed object storage drained off a Redis Stream. The interface is the same.
"""

from __future__ import annotations

from ..security.context import SecurityContext
from .bundle import ContextBundle


class InMemoryBundleStore:
    def __init__(self) -> None:
        self._by_tenant: dict[str, dict[str, ContextBundle]] = {}

    def put(self, ctx: SecurityContext, bundle: ContextBundle) -> None:
        self._by_tenant.setdefault(ctx.tenant_id, {})[bundle.trace_id] = bundle

    def get(self, ctx: SecurityContext, trace_id: str) -> ContextBundle | None:
        return self._by_tenant.get(ctx.tenant_id, {}).get(trace_id)

    def attach_output(self, ctx: SecurityContext, trace_id: str, output: str) -> bool:
        bundle = self.get(ctx, trace_id)
        if bundle is None:
            return False
        bundle.recorded_output = output  # phase-2 completion attach (post terminal event, C8)
        return True
