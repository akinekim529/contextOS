"""Security failures. All of these are fail-closed: when in doubt, deny."""

from __future__ import annotations


class SecurityError(Exception):
    """Base for fail-closed security failures (maps to HTTP 403)."""


class AccessDenied(SecurityError):
    def __init__(self, reason: str, *, principal: str | None = None, resource: str | None = None) -> None:
        self.reason = reason
        self.principal = principal
        self.resource = resource
        super().__init__(reason)


class MissingTenant(SecurityError):
    """No resolvable tenant on the request — the firewall cannot scope anything, so deny."""


class MissingNamespace(SecurityError):
    """Ambiguous/absent namespace within a tenant. C2: missing namespace = deny."""
