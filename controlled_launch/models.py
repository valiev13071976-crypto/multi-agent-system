"""Stage-4 domain models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class TrafficMode(str, Enum):
    CONTROL = "CONTROL"
    INTERNAL = "INTERNAL"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    LIMITED = "LIMITED"
    PRODUCTION = "PRODUCTION"


class CandidateStatus(str, Enum):
    DRAFT = "DRAFT"
    LOCKED = "LOCKED"
    INTERNAL = "INTERNAL"
    SHADOW = "SHADOW"
    SHADOW_PASSED = "SHADOW_PASSED"
    CANARY = "CANARY"
    LIMITED = "LIMITED"
    GO_LIVE_ELIGIBLE = "GO_LIVE_ELIGIBLE"
    ABORTED = "ABORTED"
    ROLLED_BACK = "ROLLED_BACK"


class RolloutStep(str, Enum):
    INTERNAL = "INTERNAL"
    SHADOW = "SHADOW"
    INITIAL_CANARY = "INITIAL_CANARY"
    SMALL_COHORT = "SMALL_COHORT"
    EXPANDED_COHORT = "EXPANDED_COHORT"
    LIMITED_PRODUCTION = "LIMITED_PRODUCTION"
    GO_LIVE_ELIGIBLE = "GO_LIVE_ELIGIBLE"


ROLLOUT_ORDER = (
    RolloutStep.INTERNAL,
    RolloutStep.SHADOW,
    RolloutStep.INITIAL_CANARY,
    RolloutStep.SMALL_COHORT,
    RolloutStep.EXPANDED_COHORT,
    RolloutStep.LIMITED_PRODUCTION,
    RolloutStep.GO_LIVE_ELIGIBLE,
)


class ShadowGateResult(str, Enum):
    SHADOW_PASS = "SHADOW_PASS"
    SHADOW_HOLD = "SHADOW_HOLD"
    SHADOW_FAIL = "SHADOW_FAIL"


class PromotionResult(str, Enum):
    GO_LIVE_ELIGIBLE = "GO_LIVE_ELIGIBLE"
    GO_LIVE_BLOCKED = "GO_LIVE_BLOCKED"


class VerificationClass(str, Enum):
    CODE_VERIFIED = "CODE_VERIFIED"
    CONFIG_VERIFIED = "CONFIG_VERIFIED"
    LIVE_VERIFIED = "LIVE_VERIFIED"
    BLOCKED_BY_STAGE_3 = "BLOCKED_BY_STAGE_3"
    OPERATOR_ACTION_REQUIRED = "OPERATOR_ACTION_REQUIRED"
    NOT_ENABLED = "NOT_ENABLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class LaunchCandidate:
    candidate_id: str
    commit_sha: str
    deployment_id: str
    environment: str
    production_url: str
    rollback_target: str
    stage3_evidence_id: str
    status: str = CandidateStatus.DRAFT.value
    schema_version: str = ""
    routing_policy_version: str = ""
    provider_config_versions: dict[str, str] = field(default_factory=dict)
    capacity_envelope: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc)
    created_by: str = ""
    locked_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "commit_sha": self.commit_sha,
            "deployment_id": self.deployment_id,
            "environment": self.environment,
            "production_url": self.production_url,
            "rollback_target": self.rollback_target,
            "stage3_evidence_id": self.stage3_evidence_id,
            "status": self.status,
            "schema_version": self.schema_version,
            "routing_policy_version": self.routing_policy_version,
            "provider_config_versions": dict(self.provider_config_versions),
            "capacity_envelope": dict(self.capacity_envelope),
            "created_at": self.created_at,
            "created_by": self.created_by,
            "locked_at": self.locked_at,
        }


@dataclass(frozen=True)
class Stage3Handoff:
    evidence_id: str
    stage3_status: str
    release_readiness: str
    p0_count: int
    p1_count: int
    release_identity: str
    environment: str
    commit_sha: str
    deployment_id: str
    verified_at: str
    capacity_envelope: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "stage3_status": self.stage3_status,
            "release_readiness": self.release_readiness,
            "p0_count": self.p0_count,
            "p1_count": self.p1_count,
            "release_identity": self.release_identity,
            "environment": self.environment,
            "commit_sha": self.commit_sha,
            "deployment_id": self.deployment_id,
            "verified_at": self.verified_at,
            "capacity_envelope": dict(self.capacity_envelope),
        }


@dataclass(frozen=True)
class TrafficPolicy:
    policy_id: str
    policy_version: str
    candidate_id: str
    mode: str
    percent_basis_points: int = 0
    kill_switch: bool = False
    hold: bool = False
    abort: bool = False
    internal_tenants: frozenset[str] = frozenset()
    test_tenants: frozenset[str] = frozenset()
    canary_tenants: frozenset[str] = frozenset()
    canary_users: frozenset[str] = frozenset()
    excluded_tenants: frozenset[str] = frozenset()
    workload_cohorts: frozenset[str] = frozenset()
    side_effect_allowed: bool = False
    billing_allowed: bool = False
    shadow_enabled: bool = False
    max_shadow_concurrency: int = 10
    max_shadow_requests: int = 1000
    max_shadow_cost: float = 100.0
    created_at: str = field(default_factory=_utc)
    created_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "candidate_id": self.candidate_id,
            "mode": self.mode,
            "percent_basis_points": self.percent_basis_points,
            "kill_switch": self.kill_switch,
            "hold": self.hold,
            "abort": self.abort,
            "internal_tenants": sorted(self.internal_tenants),
            "test_tenants": sorted(self.test_tenants),
            "canary_tenants": sorted(self.canary_tenants),
            "canary_users": sorted(self.canary_users),
            "excluded_tenants": sorted(self.excluded_tenants),
            "workload_cohorts": sorted(self.workload_cohorts),
            "side_effect_allowed": self.side_effect_allowed,
            "billing_allowed": self.billing_allowed,
            "shadow_enabled": self.shadow_enabled,
            "max_shadow_concurrency": self.max_shadow_concurrency,
            "max_shadow_requests": self.max_shadow_requests,
            "max_shadow_cost": self.max_shadow_cost,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }


@dataclass(frozen=True)
class TrafficDecision:
    request_id: str
    candidate_id: str
    mode: str
    control_target: bool
    candidate_target: bool
    cohort_id: str
    assignment_reason: str
    workload_class: str
    shadow_allowed: bool
    side_effect_allowed: bool
    billing_allowed: bool
    policy_version: str
    timestamp: str = field(default_factory=_utc)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "candidate_id": self.candidate_id,
            "mode": self.mode,
            "control_target": self.control_target,
            "candidate_target": self.candidate_target,
            "cohort_id": self.cohort_id,
            "assignment_reason": self.assignment_reason,
            "workload_class": self.workload_class,
            "shadow_allowed": self.shadow_allowed,
            "side_effect_allowed": self.side_effect_allowed,
            "billing_allowed": self.billing_allowed,
            "policy_version": self.policy_version,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class CanaryPlan:
    plan_id: str
    candidate_id: str
    control_release: str
    cohort: str
    traffic_allocation_basis_points: int
    max_concurrency: int
    max_requests: int
    max_duration_seconds: float
    max_cost: float
    observation_duration_seconds: float
    guardrails: dict[str, Any]
    rollback_target: str
    side_effect_policy: str
    billing_policy: str
    provider_policy: str
    approved_by: str
    created_at: str = field(default_factory=_utc)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "control_release": self.control_release,
            "cohort": self.cohort,
            "traffic_allocation_basis_points": self.traffic_allocation_basis_points,
            "max_concurrency": self.max_concurrency,
            "max_requests": self.max_requests,
            "max_duration_seconds": self.max_duration_seconds,
            "max_cost": self.max_cost,
            "observation_duration_seconds": self.observation_duration_seconds,
            "guardrails": dict(self.guardrails),
            "rollback_target": self.rollback_target,
            "side_effect_policy": self.side_effect_policy,
            "billing_policy": self.billing_policy,
            "provider_policy": self.provider_policy,
            "approved_by": self.approved_by,
            "created_at": self.created_at,
        }


@dataclass
class RolloutState:
    candidate_id: str
    current_step: str = RolloutStep.INTERNAL.value
    completed_steps: list[str] = field(default_factory=list)
    hold: bool = False
    abort: bool = False
    rolled_back: bool = False
    updated_at: str = field(default_factory=_utc)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "current_step": self.current_step,
            "completed_steps": list(self.completed_steps),
            "hold": self.hold,
            "abort": self.abort,
            "rolled_back": self.rolled_back,
            "updated_at": self.updated_at,
        }


@dataclass
class LaunchEvidence:
    evidence_id: str
    candidate_id: str
    environment: str
    policy_version: str
    gate: str
    status: str
    classification: str
    safe_metrics: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=_utc)

    @classmethod
    def create(
        cls,
        *,
        candidate_id: str,
        environment: str,
        policy_version: str,
        gate: str,
        status: str,
        classification: str,
        safe_metrics: dict[str, Any] | None = None,
    ) -> LaunchEvidence:
        return cls(
            evidence_id=f"lc-ev-{uuid.uuid4().hex[:16]}",
            candidate_id=candidate_id,
            environment=environment,
            policy_version=policy_version,
            gate=gate,
            status=status,
            classification=classification,
            safe_metrics=dict(safe_metrics or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "candidate_id": self.candidate_id,
            "environment": self.environment,
            "policy_version": self.policy_version,
            "gate": self.gate,
            "status": self.status,
            "classification": self.classification,
            "safe_metrics": dict(self.safe_metrics),
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class PromotionDecision:
    candidate_id: str
    result: str
    reason: str
    evidence_ids: tuple[str, ...]
    decided_at: str = field(default_factory=_utc)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "result": self.result,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "decided_at": self.decided_at,
        }
