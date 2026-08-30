"""Versioned GoLivePolicy — defaults inactive; never auto-activates."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class GoLivePolicyError(ValueError):
    def __init__(self, code: str, *, details: dict | None = None):
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(self.code)


@dataclass(frozen=True)
class GoLivePolicy:
    policy_id: str
    policy_version: str
    release_identity: str
    enabled: bool = False
    go_live_active: bool = False
    operator_approval_required: bool = True
    operator_approved: bool = False
    approved_by: str = ""
    activated_by: str = ""
    activated_at: str = ""
    deactivation_state: str = ""
    health_validation_required: bool = True
    budget_guard_required: bool = True
    security_required: bool = True
    observability_required: bool = True
    rollback_readiness_required: bool = True
    mandatory_gates: tuple[str, ...] = ()
    informational_gates: tuple[str, ...] = ()
    created_at: str = field(default_factory=_utc)
    created_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "release_identity": self.release_identity,
            "enabled": self.enabled,
            "go_live_active": self.go_live_active,
            "operator_approval_required": self.operator_approval_required,
            "operator_approved": self.operator_approved,
            "approved_by": self.approved_by,
            "activated_by": self.activated_by,
            "activated_at": self.activated_at,
            "deactivation_state": self.deactivation_state,
            "health_validation_required": self.health_validation_required,
            "budget_guard_required": self.budget_guard_required,
            "security_required": self.security_required,
            "observability_required": self.observability_required,
            "rollback_readiness_required": self.rollback_readiness_required,
            "mandatory_gates": list(self.mandatory_gates),
            "informational_gates": list(self.informational_gates),
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoLivePolicy:
        return cls(
            policy_id=str(data.get("policy_id") or ""),
            policy_version=str(data.get("policy_version") or ""),
            release_identity=str(data.get("release_identity") or ""),
            enabled=bool(data.get("enabled")),
            go_live_active=bool(data.get("go_live_active")),
            operator_approval_required=bool(data.get("operator_approval_required", True)),
            operator_approved=bool(data.get("operator_approved")),
            approved_by=str(data.get("approved_by") or ""),
            activated_by=str(data.get("activated_by") or ""),
            activated_at=str(data.get("activated_at") or ""),
            deactivation_state=str(data.get("deactivation_state") or ""),
            health_validation_required=bool(data.get("health_validation_required", True)),
            budget_guard_required=bool(data.get("budget_guard_required", True)),
            security_required=bool(data.get("security_required", True)),
            observability_required=bool(data.get("observability_required", True)),
            rollback_readiness_required=bool(data.get("rollback_readiness_required", True)),
            mandatory_gates=tuple(data.get("mandatory_gates") or ()),
            informational_gates=tuple(data.get("informational_gates") or ()),
            created_at=str(data.get("created_at") or _utc()),
            created_by=str(data.get("created_by") or ""),
        )


def validate_policy(policy: GoLivePolicy) -> GoLivePolicy:
    if not policy.release_identity.strip():
        raise GoLivePolicyError("release_identity_required")
    if policy.go_live_active and not policy.operator_approved:
        raise GoLivePolicyError("operator_approval_required_for_active")
    if policy.go_live_active and not policy.activated_by:
        raise GoLivePolicyError("activated_by_required")
    return policy


def create_policy(*, release_identity: str, created_by: str, mandatory_gates: tuple[str, ...] | list[str] = ()) -> GoLivePolicy:
    from production_activation.stage5_gate import INFORMATIONAL_STAGE5_GATES, MANDATORY_STAGE5_GATES

    policy = GoLivePolicy(
        policy_id=f"glp-{uuid.uuid4().hex[:12]}",
        policy_version=f"v-{uuid.uuid4().hex[:8]}",
        release_identity=release_identity,
        enabled=False,
        go_live_active=False,
        mandatory_gates=tuple(mandatory_gates or MANDATORY_STAGE5_GATES),
        informational_gates=tuple(INFORMATIONAL_STAGE5_GATES),
        created_by=created_by,
    )
    return validate_policy(policy)


def mark_activated(policy: GoLivePolicy, *, activated_by: str) -> GoLivePolicy:
    return validate_policy(
        replace(
            policy,
            enabled=True,
            go_live_active=True,
            operator_approved=True,
            approved_by=activated_by or policy.approved_by,
            activated_by=activated_by,
            activated_at=_utc(),
            deactivation_state="",
        )
    )


def mark_deactivated(policy: GoLivePolicy, *, reason: str = "deactivated") -> GoLivePolicy:
    return replace(
        policy,
        go_live_active=False,
        enabled=False,
        deactivation_state=reason,
    )
