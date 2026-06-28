"""Deny-overrides semantics and the default-deny floor."""

from __future__ import annotations

import pytest

from contextos.models.common import Action, Effect
from contextos.models.policy import PolicyRule, RBACPolicy
from contextos.security.errors import AccessDenied
from contextos.security.rbac import PolicyEngine
from helpers import make_ctx


def test_default_floor_allows_owner_same_namespace() -> None:
    engine = PolicyEngine()
    ctx = make_ctx("t1", "u1", "alpha")
    d = engine.evaluate(ctx, resource={"tenant_id": "t1", "namespace": "alpha"}, action=Action.READ)
    assert d.allowed


def test_deny_overrides_allow() -> None:
    policy = RBACPolicy(
        tenant_id="t1",
        rules=[
            PolicyRule(principal_match={"namespace": "alpha"}, resource_match={"namespace": "alpha"},
                       actions=[Action.READ], effect=Effect.ALLOW),
            PolicyRule(principal_match={"*": "*"}, resource_match={"type": "secret"},
                       actions=[Action.READ], effect=Effect.DENY),
        ],
    )
    engine = PolicyEngine(policy)
    ctx = make_ctx("t1", "u1", "alpha")
    with pytest.raises(AccessDenied):
        engine.check(ctx, resource={"tenant_id": "t1", "namespace": "alpha", "type": "secret"},
                     action=Action.READ)


def test_route_action_is_arbitrated_by_the_same_engine() -> None:
    # The model router goes through check(action=ROUTE) — one policy authority (C10).
    engine = PolicyEngine()
    ctx = make_ctx("t1", "u1", "alpha")
    engine.check(ctx, resource={"type": "model", "tenant_id": "t1"}, action=Action.ROUTE)
    with pytest.raises(AccessDenied):
        engine.check(ctx, resource={"type": "model", "tenant_id": "t2"}, action=Action.ROUTE)
