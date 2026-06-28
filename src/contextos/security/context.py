"""The SecurityContext — the single choke point every request flows through.

Nothing in the pipeline reads or writes a store without a SecurityContext. It carries the
non-null ``tenant_id`` and an explicit ``namespace`` (C2: hard, fail-closed). Resolving it
is the first thing the gateway does; losing it is impossible without an exception.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models.common import new_ulid
from .errors import MissingNamespace, MissingTenant


class Principal(BaseModel):
    id: str
    tenant_id: str
    roles: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)


class Scope(BaseModel):
    """The hard filter applied at every repository boundary."""

    tenant_id: str
    namespace: str


class SecurityContext(BaseModel):
    tenant_id: str
    principal: Principal
    namespace: str
    request_id: str = Field(default_factory=new_ulid)

    @classmethod
    def resolve(
        cls,
        *,
        tenant_id: str | None,
        user_id: str | None,
        namespace: str | None = None,
        roles: list[str] | None = None,
    ) -> SecurityContext:
        """Build a context or fail closed. Default namespace is the user's private
        partition (``user:<id>``) — explicit and isolating, never a wildcard."""
        if not tenant_id:
            raise MissingTenant("no tenant on request")
        ns = namespace or (f"user:{user_id}" if user_id else None)
        if not ns:
            raise MissingNamespace("no namespace and no user to derive one from")
        principal = Principal(
            id=user_id or "anonymous",
            tenant_id=tenant_id,
            roles=roles or [],
            attributes={"namespace": ns},
        )
        return cls(tenant_id=tenant_id, principal=principal, namespace=ns)

    def scope(self) -> Scope:
        return Scope(tenant_id=self.tenant_id, namespace=self.namespace)
