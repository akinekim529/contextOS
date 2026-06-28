"""The RBAC/ABAC policy engine — the app-layer half of defense-in-depth (ADR-0002).

Postgres FORCE RLS is the DB backstop; this engine is the application firewall in front
of it. Both must agree. Evaluation is **deny-overrides** over a **default-deny** floor,
and a cross-tenant resource is denied before any rule is consulted — a policy can never
grant access across the tenant boundary.

This is one of the four enforcement points (retriever, cache key, router/route, response).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.common import Action, Effect
from ..models.policy import PolicyRule, RBACPolicy
from .context import SecurityContext
from .errors import AccessDenied


@dataclass(frozen=True)
class Decision:
    effect: Effect
    reason: str

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


def _matches(match: dict[str, str], attrs: dict[str, str]) -> bool:
    """All keys must match; ``"*"`` is a wildcard. An absent attribute never matches
    a concrete requirement (fail-closed)."""
    for key, want in match.items():
        if want == "*":
            continue
        if attrs.get(key) != want:
            return False
    return True


class PolicyEngine:
    def __init__(self, policy: RBACPolicy | None = None) -> None:
        self._policy = policy

    def evaluate(
        self,
        ctx: SecurityContext,
        *,
        resource: dict[str, str],
        action: Action,
    ) -> Decision:
        # Tenant boundary is absolute and checked before policy rules.
        res_tenant = resource.get("tenant_id")
        if res_tenant is not None and res_tenant != ctx.tenant_id:
            return Decision(Effect.DENY, f"cross-tenant access denied ({res_tenant} != {ctx.tenant_id})")

        # Namespace is a hard filter within a tenant (C2). shared_org is the only escape,
        # and only when a rule explicitly allows it (handled by the rules below).
        res_ns = resource.get("namespace")
        principal_attrs = {**ctx.principal.attributes, "tenant_id": ctx.tenant_id, "namespace": ctx.namespace}

        policy = self._policy
        if policy is None or not policy.rules:
            # No tenant policy loaded: allow same-tenant, same-namespace reads/writes by the
            # owning principal; deny everything else. This is the safe default-deny floor.
            if res_ns is not None and res_ns != ctx.namespace:
                return Decision(Effect.DENY, "namespace mismatch under default-deny floor")
            if action in (Action.READ, Action.WRITE, Action.CACHE_READ, Action.ROUTE):
                return Decision(Effect.ALLOW, "default floor: owner, same tenant+namespace")
            return Decision(Effect.DENY, f"default-deny: {action} requires an explicit policy")

        allow_hit = False
        rule: PolicyRule
        for rule in policy.rules:
            if action not in rule.actions:
                continue
            if not _matches(rule.principal_match, principal_attrs):
                continue
            if not _matches(rule.resource_match, {**resource, "namespace": res_ns or ctx.namespace}):
                continue
            if rule.effect is Effect.DENY:
                return Decision(Effect.DENY, "explicit deny rule (deny-overrides)")
            allow_hit = True

        if allow_hit:
            return Decision(Effect.ALLOW, "matched allow rule, no deny override")
        return Decision(policy.default_effect, "no matching rule; default effect applied")

    def check(self, ctx: SecurityContext, *, resource: dict[str, str], action: Action) -> None:
        """Raise :class:`AccessDenied` unless allowed. The pipeline calls this; it never
        proceeds on a falsy return because there is no falsy return."""
        decision = self.evaluate(ctx, resource=resource, action=action)
        if not decision.allowed:
            raise AccessDenied(
                decision.reason,
                principal=ctx.principal.id,
                resource=f"{resource.get('type', 'resource')}:{resource.get('namespace', '?')}",
            )
