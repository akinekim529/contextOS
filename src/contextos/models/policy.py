"""RBAC/ABAC policy schema. Evaluation is deny-overrides with a default-deny floor."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import Action, Effect, new_ulid


class PolicyRule(BaseModel):
    """Attribute-based rule. ``*_match`` maps are ANDed; a value of ``"*"`` matches anything.

    Match keys are evaluated against the principal's attributes and the resource's
    attributes (tenant_id, namespace, type, visibility, model, residency, ...).
    """

    principal_match: dict[str, str] = Field(default_factory=dict)
    resource_match: dict[str, str] = Field(default_factory=dict)
    actions: list[Action]
    effect: Effect


class RBACPolicy(BaseModel):
    id: str = Field(default_factory=new_ulid)
    tenant_id: str
    description: str = ""
    rules: list[PolicyRule] = Field(default_factory=list)
    default_effect: Effect = Effect.DENY  # fail closed
