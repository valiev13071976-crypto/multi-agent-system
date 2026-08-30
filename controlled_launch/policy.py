"""Stage-4 Controlled Launch policy — versioned, bounded, fail-closed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class ControlledLaunchPolicyError(ValueError):
    def __init__(self, code: str, *, details: dict | None = None):
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(self.code)


@dataclass(frozen=True)
class ControlledLaunchPolicy:
    """Explicit versioned Stage-4 launch policy. Defaults disabled."""

    policy_id: str
    policy_version: str
    release_identity: str
    enabled: bool = False
    tenant_allowlist: frozenset[str] = frozenset()
    user_allowlist: frozenset[str] = frozenset()
    max_cohort_size: int = 0
    max_traffic_percent: int = 0
    max_interactive_concurrency: int = 0
    max_batch_concurrency: int = 0
    per_tenant_concurrency: int = 0
    per_tenant_rate_per_minute: int = 0
    budget_ceiling: float = 0.0
    budget_warning_threshold: float = 0.0
    kill_switch: bool = False
    containment_action: str = "CONTINUE"
    restricted_capabilities: frozenset[str] = frozenset()
    operator_approved: bool = False
    approved_by: str = ""
    start_at: str = ""
    expires_at: str = ""
    created_at: str = field(default_factory=_utc)
    created_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "release_identity": self.release_identity,
            "enabled": self.enabled,
            "tenant_allowlist": sorted(self.tenant_allowlist),
            "user_allowlist": sorted(self.user_allowlist),
            "max_cohort_size": self.max_cohort_size,
            "max_traffic_percent": self.max_traffic_percent,
            "max_interactive_concurrency": self.max_interactive_concurrency,
            "max_batch_concurrency": self.max_batch_concurrency,
            "per_tenant_concurrency": self.per_tenant_concurrency,
            "per_tenant_rate_per_minute": self.per_tenant_rate_per_minute,
            "budget_ceiling": self.budget_ceiling,
            "budget_warning_threshold": self.budget_warning_threshold,
            "kill_switch": self.kill_switch,
            "containment_action": self.containment_action,
            "restricted_capabilities": sorted(self.restricted_capabilities),
            "operator_approved": self.operator_approved,
            "approved_by": self.approved_by,
            "start_at": self.start_at,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ControlledLaunchPolicy:
        return cls(
            policy_id=str(data.get("policy_id") or ""),
            policy_version=str(data.get("policy_version") or ""),
            release_identity=str(data.get("release_identity") or ""),
            enabled=bool(data.get("enabled")),
            tenant_allowlist=frozenset(data.get("tenant_allowlist") or []),
            user_allowlist=frozenset(data.get("user_allowlist") or []),
            max_cohort_size=int(data.get("max_cohort_size") or 0),
            max_traffic_percent=int(data.get("max_traffic_percent") or 0),
            max_interactive_concurrency=int(data.get("max_interactive_concurrency") or 0),
            max_batch_concurrency=int(data.get("max_batch_concurrency") or 0),
            per_tenant_concurrency=int(data.get("per_tenant_concurrency") or 0),
            per_tenant_rate_per_minute=int(data.get("per_tenant_rate_per_minute") or 0),
            budget_ceiling=float(data.get("budget_ceiling") or 0.0),
            budget_warning_threshold=float(data.get("budget_warning_threshold") or 0.0),
            kill_switch=bool(data.get("kill_switch")),
            containment_action=str(data.get("containment_action") or "CONTINUE"),
            restricted_capabilities=frozenset(data.get("restricted_capabilities") or []),
            operator_approved=bool(data.get("operator_approved")),
            approved_by=str(data.get("approved_by") or ""),
            start_at=str(data.get("start_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
            created_at=str(data.get("created_at") or _utc()),
            created_by=str(data.get("created_by") or ""),
        )


def validate_policy(policy: ControlledLaunchPolicy) -> ControlledLaunchPolicy:
    if not policy.release_identity.strip():
        raise ControlledLaunchPolicyError("release_identity_required")
    if policy.max_traffic_percent < 0 or policy.max_traffic_percent > 100:
        raise ControlledLaunchPolicyError("invalid_traffic_percent", details={"value": policy.max_traffic_percent})
    if policy.max_cohort_size < 0:
        raise ControlledLaunchPolicyError("invalid_cohort_size")
    if policy.max_interactive_concurrency < 0 or policy.max_batch_concurrency < 0:
        raise ControlledLaunchPolicyError("invalid_concurrency")
    if policy.per_tenant_concurrency < 0 or policy.per_tenant_rate_per_minute < 0:
        raise ControlledLaunchPolicyError("invalid_tenant_quota")
    if policy.budget_ceiling < 0 or policy.budget_warning_threshold < 0:
        raise ControlledLaunchPolicyError("invalid_budget")
    if policy.budget_warning_threshold and policy.budget_ceiling and policy.budget_warning_threshold > policy.budget_ceiling:
        raise ControlledLaunchPolicyError("warning_exceeds_ceiling")
    if policy.containment_action not in {"CONTINUE", "DEGRADE", "PAUSE_ADMISSION", "KILL_CONTROLLED_LAUNCH"}:
        raise ControlledLaunchPolicyError("invalid_containment_action", details={"value": policy.containment_action})
    if policy.enabled and not policy.operator_approved:
        raise ControlledLaunchPolicyError("operator_approval_required")
    if policy.enabled and policy.kill_switch:
        raise ControlledLaunchPolicyError("cannot_enable_with_kill_switch")
    if policy.enabled and policy.max_cohort_size <= 0 and not (policy.tenant_allowlist or policy.user_allowlist):
        raise ControlledLaunchPolicyError("cohort_required_when_enabled")
    return policy


def create_policy(
    *,
    release_identity: str,
    created_by: str,
    tenant_allowlist: frozenset[str] | set[str] | list[str] = (),
    user_allowlist: frozenset[str] | set[str] | list[str] = (),
    max_cohort_size: int = 10,
    max_traffic_percent: int = 5,
    max_interactive_concurrency: int = 5,
    max_batch_concurrency: int = 2,
    per_tenant_concurrency: int = 2,
    per_tenant_rate_per_minute: int = 60,
    budget_ceiling: float = 50.0,
    budget_warning_threshold: float = 25.0,
    restricted_capabilities: frozenset[str] | set[str] | list[str] = (),
) -> ControlledLaunchPolicy:
    policy = ControlledLaunchPolicy(
        policy_id=f"clp-{uuid.uuid4().hex[:12]}",
        policy_version=f"v-{uuid.uuid4().hex[:8]}",
        release_identity=release_identity,
        enabled=False,
        tenant_allowlist=frozenset(tenant_allowlist),
        user_allowlist=frozenset(user_allowlist),
        max_cohort_size=max_cohort_size,
        max_traffic_percent=max_traffic_percent,
        max_interactive_concurrency=max_interactive_concurrency,
        max_batch_concurrency=max_batch_concurrency,
        per_tenant_concurrency=per_tenant_concurrency,
        per_tenant_rate_per_minute=per_tenant_rate_per_minute,
        budget_ceiling=budget_ceiling,
        budget_warning_threshold=budget_warning_threshold,
        restricted_capabilities=frozenset(restricted_capabilities),
        created_by=created_by,
    )
    return validate_policy(policy)


def with_kill_switch(policy: ControlledLaunchPolicy, *, enabled: bool, actor: str = "") -> ControlledLaunchPolicy:
    return replace(
        policy,
        kill_switch=bool(enabled),
        enabled=False if enabled else policy.enabled,
        approved_by=actor or policy.approved_by,
    )


def activate_policy(policy: ControlledLaunchPolicy, *, approved_by: str) -> ControlledLaunchPolicy:
    activated = replace(
        policy,
        enabled=True,
        operator_approved=True,
        approved_by=approved_by,
        kill_switch=False,
        start_at=_utc(),
    )
    return validate_policy(activated)


def pause_policy(policy: ControlledLaunchPolicy) -> ControlledLaunchPolicy:
    return replace(policy, enabled=False)
