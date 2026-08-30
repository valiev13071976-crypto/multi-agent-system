"""Canary plan and bounded execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from controlled_launch.errors import CANARY_PRECONDITION_FAILED, ControlledLaunchError
from controlled_launch.models import CanaryPlan, CandidateStatus, LaunchCandidate, LaunchEvidence, VerificationClass
from controlled_launch.shadow import ShadowGateResult
from evals.canary import CanaryController


@dataclass
class CanaryObservation:
    candidate_requests: int = 0
    control_requests: int = 0
    candidate_errors: int = 0
    control_errors: int = 0
    candidate_latency_p50_ms: float | None = None
    candidate_latency_p95_ms: float | None = None
    control_latency_p50_ms: float | None = None
    control_latency_p95_ms: float | None = None
    provider_failures: int = 0
    fallbacks: int = 0
    total_cost: float = 0.0
    started_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_requests": self.candidate_requests,
            "control_requests": self.control_requests,
            "candidate_errors": self.candidate_errors,
            "control_errors": self.control_errors,
            "candidate_latency_p50_ms": self.candidate_latency_p50_ms,
            "candidate_latency_p95_ms": self.candidate_latency_p95_ms,
            "control_latency_p50_ms": self.control_latency_p50_ms,
            "control_latency_p95_ms": self.control_latency_p95_ms,
            "provider_failures": self.provider_failures,
            "fallbacks": self.fallbacks,
            "total_cost": self.total_cost,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


@dataclass
class CanaryControllerService:
    controller: CanaryController = field(default_factory=CanaryController)
    observation: CanaryObservation = field(default_factory=CanaryObservation)
    active_plan: CanaryPlan | None = None

    def prepare(
        self,
        *,
        candidate: LaunchCandidate,
        plan: CanaryPlan,
        shadow_gate: ShadowGateResult,
        monitoring_ready: bool = True,
        alerts_ready: bool = True,
        backup_ready: bool = True,
    ) -> None:
        if candidate.status not in {CandidateStatus.LOCKED.value, CandidateStatus.SHADOW_PASSED.value, CandidateStatus.SHADOW.value}:
            raise ControlledLaunchError(CANARY_PRECONDITION_FAILED, details={"status": candidate.status})
        if not candidate.rollback_target:
            raise ControlledLaunchError(CANARY_PRECONDITION_FAILED, details={"rollback_target": "missing"})
        if shadow_gate != ShadowGateResult.SHADOW_PASS:
            raise ControlledLaunchError(CANARY_PRECONDITION_FAILED, details={"shadow_gate": shadow_gate.value})
        if not (monitoring_ready and alerts_ready and backup_ready):
            raise ControlledLaunchError(CANARY_PRECONDITION_FAILED, details={"monitoring": monitoring_ready, "alerts": alerts_ready})
        self.active_plan = plan

    def start(self, *, now: datetime | None = None) -> None:
        plan = self.active_plan
        if plan is None:
            raise ControlledLaunchError(CANARY_PRECONDITION_FAILED, details={"plan": "missing"})
        percent = max(1, plan.traffic_allocation_basis_points // 100)
        self.controller.enable(plan.candidate_id, percent, plan.plan_id, now=now)
        self.observation = CanaryObservation(started_at=now or datetime.now(timezone.utc))

    def within_bounds(self) -> bool:
        plan = self.active_plan
        if plan is None:
            return False
        if self.observation.candidate_requests >= plan.max_requests:
            return False
        if self.observation.total_cost >= plan.max_cost:
            return False
        if self.observation.started_at is not None:
            elapsed = (datetime.now(timezone.utc) - self.observation.started_at).total_seconds()
            if elapsed > plan.max_duration_seconds:
                return False
        return True

    def record_request(self, *, candidate: bool, error: bool = False, latency_ms: float | None = None, cost: float = 0.0) -> None:
        if candidate:
            self.observation.candidate_requests += 1
            if error:
                self.observation.candidate_errors += 1
        else:
            self.observation.control_requests += 1
            if error:
                self.observation.control_errors += 1
        self.observation.total_cost += cost

    def stop(self) -> LaunchEvidence | None:
        self.controller.disable()
        plan = self.active_plan
        if plan is None:
            return None
        return LaunchEvidence.create(
            candidate_id=plan.candidate_id,
            environment="",
            policy_version=plan.plan_id,
            gate="4.8_canary_observation",
            status="PASS" if self.within_bounds() else "HOLD",
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics=self.observation.as_dict(),
        )
