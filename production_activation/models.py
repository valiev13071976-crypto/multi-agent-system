"""Stage-5 domain models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActivationState(str, Enum):
    GO_LIVE_ELIGIBLE = "GO_LIVE_ELIGIBLE"
    ACTIVATION_PENDING = "ACTIVATION_PENDING"
    ACTIVATING = "ACTIVATING"
    PRODUCTION_ACTIVE = "PRODUCTION_ACTIVE"
    DEGRADED = "DEGRADED"
    ROLLBACK_PENDING = "ROLLBACK_PENDING"
    ROLLED_BACK = "ROLLED_BACK"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"


class AcceptanceResult(str, Enum):
    PRODUCTION_ACCEPTED = "PRODUCTION_ACCEPTED"
    PRODUCTION_UNSTABLE = "PRODUCTION_UNSTABLE"
    ROLLED_BACK = "ROLLED_BACK"
    BLOCKED = "BLOCKED"


class VerificationClass(str, Enum):
    CODE_VERIFIED = "CODE_VERIFIED"
    CONFIG_VERIFIED = "CONFIG_VERIFIED"
    LIVE_VERIFIED = "LIVE_VERIFIED"
    BLOCKED_BY_PREVIOUS_STAGE = "BLOCKED_BY_PREVIOUS_STAGE"
    OPERATOR_ACTION_REQUIRED = "OPERATOR_ACTION_REQUIRED"
    NOT_ENABLED = "NOT_ENABLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ProviderRequirement(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    NOT_ENABLED = "NOT_ENABLED"


class SideEffectMode(str, Enum):
    REQUIRED_LIVE = "REQUIRED_LIVE"
    SANDBOX = "SANDBOX"
    DISABLED = "DISABLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class Stage5Handoff:
    stage3_status: str
    stage3_readiness: str
    stage3_p0: int
    stage3_p1: int
    stage3_evidence_id: str
    stage4_status: str
    promotion_decision: str
    stage4_p0: int
    stage4_p1: int
    stage4_evidence_id: str
    candidate_id: str
    commit_sha: str
    deployment_id: str
    environment: str
    rollback_target: str
    monitoring_ready: bool
    alerts_ready: bool
    backup_ready: bool
    verified_at: str = field(default_factory=_utc)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage3_status": self.stage3_status,
            "stage3_readiness": self.stage3_readiness,
            "stage3_p0": self.stage3_p0,
            "stage3_p1": self.stage3_p1,
            "stage3_evidence_id": self.stage3_evidence_id,
            "stage4_status": self.stage4_status,
            "promotion_decision": self.promotion_decision,
            "stage4_p0": self.stage4_p0,
            "stage4_p1": self.stage4_p1,
            "stage4_evidence_id": self.stage4_evidence_id,
            "candidate_id": self.candidate_id,
            "commit_sha": self.commit_sha,
            "deployment_id": self.deployment_id,
            "environment": self.environment,
            "rollback_target": self.rollback_target,
            "monitoring_ready": self.monitoring_ready,
            "alerts_ready": self.alerts_ready,
            "backup_ready": self.backup_ready,
            "verified_at": self.verified_at,
        }


@dataclass(frozen=True)
class FinalProductionCandidate:
    candidate_id: str
    commit_sha: str
    deployment_id: str
    environment: str
    production_url: str
    rollback_target: str
    stage3_evidence_id: str
    stage4_evidence_id: str
    schema_version: str = ""
    routing_policy_version: str = ""
    traffic_policy_version: str = ""
    provider_config_versions: dict[str, str] = field(default_factory=dict)
    capacity_envelope: dict[str, Any] = field(default_factory=dict)
    cost_envelope: dict[str, Any] = field(default_factory=dict)
    monitoring_state: str = "unknown"
    alert_state: str = "unknown"
    backup_state: str = "unknown"
    locked_at: str = field(default_factory=_utc)
    fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "commit_sha": self.commit_sha,
            "deployment_id": self.deployment_id,
            "environment": self.environment,
            "production_url": self.production_url,
            "rollback_target": self.rollback_target,
            "stage3_evidence_id": self.stage3_evidence_id,
            "stage4_evidence_id": self.stage4_evidence_id,
            "schema_version": self.schema_version,
            "routing_policy_version": self.routing_policy_version,
            "traffic_policy_version": self.traffic_policy_version,
            "provider_config_versions": dict(self.provider_config_versions),
            "capacity_envelope": dict(self.capacity_envelope),
            "cost_envelope": dict(self.cost_envelope),
            "monitoring_state": self.monitoring_state,
            "alert_state": self.alert_state,
            "backup_state": self.backup_state,
            "locked_at": self.locked_at,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class GoLivePlan:
    plan_id: str
    candidate_id: str
    environment: str
    activation_window: str
    authorized_operator: str
    traffic_transition: str
    launch_required_providers: tuple[str, ...]
    billing_mode: str
    side_effect_policy: dict[str, str]
    expected_capacity: dict[str, Any]
    cost_envelope: dict[str, Any]
    smoke_plan: tuple[str, ...]
    hypercare_policy: dict[str, Any]
    abort_conditions: tuple[str, ...]
    rollback_conditions: tuple[str, ...]
    rollback_target: str
    monitoring_destination: str
    alert_destination: str
    backup_state: str
    incident_owner: str
    status: str = "DRAFT"
    created_at: str = field(default_factory=_utc)
    fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "environment": self.environment,
            "activation_window": self.activation_window,
            "authorized_operator": self.authorized_operator,
            "traffic_transition": self.traffic_transition,
            "launch_required_providers": list(self.launch_required_providers),
            "billing_mode": self.billing_mode,
            "side_effect_policy": dict(self.side_effect_policy),
            "expected_capacity": dict(self.expected_capacity),
            "cost_envelope": dict(self.cost_envelope),
            "smoke_plan": list(self.smoke_plan),
            "hypercare_policy": dict(self.hypercare_policy),
            "abort_conditions": list(self.abort_conditions),
            "rollback_conditions": list(self.rollback_conditions),
            "rollback_target": self.rollback_target,
            "monitoring_destination": self.monitoring_destination,
            "alert_destination": self.alert_destination,
            "backup_state": self.backup_state,
            "incident_owner": self.incident_owner,
            "status": self.status,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ActivationAuthorization:
    authorization_id: str
    candidate_fingerprint: str
    deployment_fingerprint: str
    plan_fingerprint: str
    operator_ref: str
    confirmation_token: str
    idempotency_key: str
    issued_at: str
    expires_at: str
    consumed: bool = False
    consumed_at: str = ""
    attempt_id: str = ""
    candidate_id: str = ""
    plan_id: str = ""
    release_identity: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "authorization_id": self.authorization_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "deployment_fingerprint": self.deployment_fingerprint,
            "plan_fingerprint": self.plan_fingerprint,
            "operator_ref": self.operator_ref,
            "confirmation_token": self.confirmation_token,
            "idempotency_key": self.idempotency_key,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "consumed": self.consumed,
            "consumed_at": self.consumed_at,
            "attempt_id": self.attempt_id,
            "candidate_id": self.candidate_id,
            "plan_id": self.plan_id,
            "release_identity": self.release_identity,
        }


@dataclass
class ActivationAttempt:
    attempt_id: str
    candidate_id: str
    plan_id: str
    authorization_id: str
    operator_ref: str
    state: str
    started_at: str = field(default_factory=_utc)
    completed_at: str = ""
    routing_result: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    idempotency_key: str = ""
    already_applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "candidate_id": self.candidate_id,
            "plan_id": self.plan_id,
            "authorization_id": self.authorization_id,
            "operator_ref": self.operator_ref,
            "state": self.state,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "routing_result": dict(self.routing_result),
            "error_code": self.error_code,
            "idempotency_key": self.idempotency_key,
            "already_applied": self.already_applied,
        }


@dataclass
class ProductionActivationEvidence:
    evidence_id: str
    candidate_id: str
    deployment_id: str
    environment: str
    plan_id: str
    attempt_id: str
    activation_state: str
    acceptance_result: str
    classification: str
    safe_metrics: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=_utc)

    @classmethod
    def create(cls, **kwargs) -> ProductionActivationEvidence:
        return cls(evidence_id=f"pa-ev-{uuid.uuid4().hex[:16]}", **kwargs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "candidate_id": self.candidate_id,
            "deployment_id": self.deployment_id,
            "environment": self.environment,
            "plan_id": self.plan_id,
            "attempt_id": self.attempt_id,
            "activation_state": self.activation_state,
            "acceptance_result": self.acceptance_result,
            "classification": self.classification,
            "safe_metrics": dict(self.safe_metrics),
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class ProductionAcceptanceDecision:
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
