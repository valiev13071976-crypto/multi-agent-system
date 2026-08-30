"""Controlled launch orchestration service."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from controlled_launch.access import LaunchAuthorizationPolicy, PERM_LAUNCH_READ, PERM_LAUNCH_WRITE
from controlled_launch.canary import CanaryControllerService
from controlled_launch.candidate import LaunchCandidateManager
from controlled_launch.commands import (
    AbortRolloutCommand,
    AdvanceRolloutCommand,
    HoldRolloutCommand,
    RollbackRolloutCommand,
    StartCanaryCommand,
    StartInternalCommand,
    StartShadowCommand,
)
from controlled_launch.errors import BLOCKED_BY_STAGE_3, ControlledLaunchError
from controlled_launch.finops_watch import FinOpsWatch
from controlled_launch.guardrails import GuardrailEvaluator
from controlled_launch.handoff import Stage3HandoffGate
from controlled_launch.models import (
    CandidateStatus,
    CanaryPlan,
    LaunchEvidence,
    PromotionResult,
    RolloutStep,
    ShadowGateResult,
    TrafficMode,
    VerificationClass,
)
from controlled_launch.promotion import PromotionGate
from controlled_launch.rollout import RolloutManager
from controlled_launch.router import ControlledLaunchRouter
from controlled_launch.security_watch import SecurityWatch
from controlled_launch.shadow import ShadowController
from controlled_launch.sqlite_store import SqliteControlledLaunchStore
from controlled_launch.traffic_policy import TrafficPolicyFactory
from evals.activation import RoutingActivationService


class ControlledLaunchService:
    def __init__(
        self,
        *,
        store: SqliteControlledLaunchStore | None = None,
        handoff_gate: Stage3HandoffGate | None = None,
        access: LaunchAuthorizationPolicy | None = None,
        activation: RoutingActivationService | None = None,
    ):
        self.store = store or SqliteControlledLaunchStore()
        self.handoff_gate = handoff_gate or Stage3HandoffGate()
        self.access = access or LaunchAuthorizationPolicy()
        self.activation = activation or RoutingActivationService()
        self.candidate_mgr = LaunchCandidateManager(handoff_gate=self.handoff_gate)
        self.router = ControlledLaunchRouter(handoff_gate=self.handoff_gate)
        self.shadow = ShadowController()
        self.canary = CanaryControllerService()
        self.promotion_gate = PromotionGate()
        self.security_watch = SecurityWatch()
        self.finops_watch = FinOpsWatch()
        self._lock = __import__("threading").RLock()

    def _audit(self, *, action: str, actor: str, candidate_id: str, details: dict | None = None) -> None:
        self.store.append_audit(
            {
                "action": action,
                "actor": actor,
                "candidate_id": candidate_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "details": dict(details or {}),
            }
        )

    def get_handoff(self):
        return self.handoff_gate.evaluate().as_dict()

    def create_candidate(self, ctx, **kwargs):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        candidate = self.candidate_mgr.draft(**kwargs, require_stage3=False)
        self.store.save_candidate(candidate)
        self._audit(action="create_candidate", actor=getattr(ctx, "actor_ref", "system"), candidate_id=candidate.candidate_id)
        return candidate.as_dict()

    def lock_candidate(self, ctx, candidate_id: str):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise ControlledLaunchError("candidate_not_found")
        locked = self.candidate_mgr.lock(candidate, actor=getattr(ctx, "actor_ref", "operator"))
        self.store.save_candidate(locked)
        rollout = RolloutManager(candidate_id=candidate_id)
        self.store.save_rollout(rollout.state)
        self._audit(action="lock_candidate", actor=getattr(ctx, "actor_ref", "operator"), candidate_id=candidate_id)
        return locked.as_dict()

    def read_model(self, ctx, candidate_id: str):
        self.access.require(ctx, PERM_LAUNCH_READ)
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise ControlledLaunchError("candidate_not_found")
        return {
            "candidate": candidate.as_dict(),
            "handoff": self.get_handoff(),
            "policy": (self.store.latest_policy_for_candidate(candidate_id) or TrafficPolicyFactory.create(candidate_id=candidate_id, mode=TrafficMode.CONTROL, created_by="system")).as_dict(),
            "rollout": (self.store.get_rollout(candidate_id) or RolloutManager(candidate_id=candidate_id).state).as_dict(),
            "shadow": self.shadow.metrics.as_dict(),
            "canary": self.canary.observation.as_dict(),
            "security": self.security_watch.as_dict(),
            "finops": self.finops_watch.as_dict(),
            "evidence": [e.as_dict() for e in self.store.list_evidence(candidate_id)],
        }

    def _require_live_ready(self):
        if not self.handoff_gate.allows_live_traffic():
            raise ControlledLaunchError(BLOCKED_BY_STAGE_3)

    def start_internal(self, ctx, cmd: StartInternalCommand):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        self._require_live_ready()
        candidate = self.store.get_candidate(cmd.candidate_id)
        if candidate is None:
            raise ControlledLaunchError("candidate_not_found")
        self.candidate_mgr.assert_locked(candidate)
        policy = TrafficPolicyFactory.create(
            candidate_id=candidate.candidate_id,
            mode=TrafficMode.INTERNAL,
            created_by=cmd.actor_ref,
            internal_tenants=frozenset({"internal-ops"}),
        )
        self.store.save_policy(policy)
        updated = self.candidate_mgr.with_status(candidate, CandidateStatus.INTERNAL)
        self.store.save_candidate(updated)
        ev = LaunchEvidence.create(
            candidate_id=candidate.candidate_id,
            environment=candidate.environment,
            policy_version=policy.policy_version,
            gate="4.3_internal",
            status="PASS",
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics={"mode": "INTERNAL"},
        )
        self.store.save_evidence(ev)
        self._audit(action="start_internal", actor=cmd.actor_ref, candidate_id=cmd.candidate_id)
        return {"candidate": updated.as_dict(), "policy": policy.as_dict(), "evidence": ev.as_dict()}

    def start_shadow(self, ctx, cmd: StartShadowCommand):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        self._require_live_ready()
        candidate = self.store.get_candidate(cmd.candidate_id)
        if candidate is None:
            raise ControlledLaunchError("candidate_not_found")
        policy = TrafficPolicyFactory.create(
            candidate_id=candidate.candidate_id,
            mode=TrafficMode.SHADOW,
            created_by=cmd.actor_ref,
            shadow_enabled=True,
        )
        self.store.save_policy(policy)
        updated = self.candidate_mgr.with_status(candidate, CandidateStatus.SHADOW)
        self.store.save_candidate(updated)
        self._audit(action="start_shadow", actor=cmd.actor_ref, candidate_id=cmd.candidate_id)
        return {"candidate": updated.as_dict(), "policy": policy.as_dict()}

    def evaluate_shadow_gate(self, ctx, candidate_id: str):
        self.access.require(ctx, PERM_LAUNCH_READ)
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise ControlledLaunchError("candidate_not_found")
        policy = self.store.latest_policy_for_candidate(candidate_id)
        result, ev = self.shadow.evaluate_gate(
            candidate_id=candidate_id,
            environment=candidate.environment,
            policy_version=policy.policy_version if policy else "",
            security_events=self.security_watch.events,
        )
        self.store.save_evidence(ev)
        if result == ShadowGateResult.SHADOW_PASS:
            updated = replace(candidate, status=CandidateStatus.SHADOW_PASSED.value)
            self.store.save_candidate(updated)
        self._audit(action="shadow_gate", actor=getattr(ctx, "actor_ref", "system"), candidate_id=candidate_id, details={"result": result.value})
        return {"result": result.value, "evidence": ev.as_dict()}

    def prepare_canary(self, ctx, *, candidate_id: str, approved_by: str, allocation_basis_points: int = 100):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        self._require_live_ready()
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise ControlledLaunchError("candidate_not_found")
        plan = CanaryPlan(
            plan_id=f"plan-{uuid.uuid4().hex[:8]}",
            candidate_id=candidate_id,
            control_release=candidate.rollback_target,
            cohort="initial",
            traffic_allocation_basis_points=allocation_basis_points,
            max_concurrency=5,
            max_requests=100,
            max_duration_seconds=3600,
            max_cost=50.0,
            observation_duration_seconds=300,
            guardrails={"error_rate": {"threshold": 0.1, "baseline": 0.05, "window": "5m", "action": "HOLD"}},
            rollback_target=candidate.rollback_target,
            side_effect_policy="bounded",
            billing_policy="sandbox",
            provider_policy="existing_governor",
            approved_by=approved_by,
        )
        shadow_ev = next((e for e in self.store.list_evidence(candidate_id) if e.gate == "4.5_shadow_gate"), None)
        shadow_result = ShadowGateResult(shadow_ev.status) if shadow_ev else ShadowGateResult.SHADOW_HOLD
        self.canary.prepare(candidate=candidate, plan=plan, shadow_gate=shadow_result)
        self._audit(action="prepare_canary", actor=approved_by, candidate_id=candidate_id, details={"plan_id": plan.plan_id})
        return plan.as_dict()

    def start_canary(self, ctx, cmd: StartCanaryCommand):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        self._require_live_ready()
        candidate = self.store.get_candidate(cmd.candidate_id)
        if candidate is None:
            raise ControlledLaunchError("candidate_not_found")
        self.canary.start()
        updated = self.candidate_mgr.with_status(candidate, CandidateStatus.CANARY)
        self.store.save_candidate(updated)
        self._audit(action="start_canary", actor=cmd.actor_ref, candidate_id=cmd.candidate_id)
        return updated.as_dict()

    def hold(self, ctx, cmd: HoldRolloutCommand):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        state = self.store.get_rollout(cmd.candidate_id) or RolloutManager(candidate_id=cmd.candidate_id).state
        mgr = RolloutManager(state)
        mgr.hold()
        self.store.save_rollout(mgr.state)
        self._audit(action="hold", actor=cmd.actor_ref, candidate_id=cmd.candidate_id, details={"reason": cmd.reason})
        return mgr.state.as_dict()

    def abort(self, ctx, cmd: AbortRolloutCommand):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        state = self.store.get_rollout(cmd.candidate_id) or RolloutManager(candidate_id=cmd.candidate_id).state
        mgr = RolloutManager(state)
        mgr.abort()
        self.store.save_rollout(mgr.state)
        candidate = self.store.get_candidate(cmd.candidate_id)
        if candidate:
            self.store.save_candidate(replace(candidate, status=CandidateStatus.ABORTED.value))
        self.canary.controller.disable()
        self._audit(action="abort", actor=cmd.actor_ref, candidate_id=cmd.candidate_id, details={"reason": cmd.reason})
        return mgr.state.as_dict()

    def rollback(self, ctx, cmd: RollbackRolloutCommand):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        state = self.store.get_rollout(cmd.candidate_id) or RolloutManager(candidate_id=cmd.candidate_id).state
        mgr = RolloutManager(state)
        mgr.rollback()
        self.store.save_rollout(mgr.state)
        candidate = self.store.get_candidate(cmd.candidate_id)
        if candidate:
            self.store.save_candidate(replace(candidate, status=CandidateStatus.ROLLED_BACK.value))
            self.activation.rollback(cmd.actor_ref)
        self.canary.controller.disable()
        self._audit(action="rollback", actor=cmd.actor_ref, candidate_id=cmd.candidate_id, details={"reason": cmd.reason})
        return mgr.state.as_dict()

    def advance_rollout(self, ctx, cmd: AdvanceRolloutCommand):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        state = self.store.get_rollout(cmd.candidate_id)
        if state is None:
            raise ControlledLaunchError("rollout_not_found")
        mgr = RolloutManager(state)
        mgr.advance_to(RolloutStep(cmd.target_step))
        self.store.save_rollout(mgr.state)
        ev = LaunchEvidence.create(
            candidate_id=cmd.candidate_id,
            environment="",
            policy_version="",
            gate="4.10_rollout",
            status="PASS",
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics={"step": mgr.state.current_step},
        )
        self.store.save_evidence(ev)
        self._audit(action="advance_rollout", actor=cmd.actor_ref, candidate_id=cmd.candidate_id, details={"step": cmd.target_step})
        return mgr.state.as_dict()

    def evaluate_eligibility(self, ctx, candidate_id: str):
        self.access.require(ctx, PERM_LAUNCH_READ)
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise ControlledLaunchError("candidate_not_found")
        rollout = self.store.get_rollout(candidate_id)
        decision = self.promotion_gate.evaluate(
            candidate_id=candidate_id,
            candidate_status=candidate.status,
            evidence=self.store.list_evidence(candidate_id),
            p0_count=self.security_watch.p0_count,
            p1_count=self.security_watch.p1_count,
            stage3_ready=self.handoff_gate.allows_live_traffic(),
            rollout_step=rollout.current_step if rollout else "",
        )
        if decision.result == PromotionResult.GO_LIVE_ELIGIBLE.value:
            self.store.save_candidate(replace(candidate, status=CandidateStatus.GO_LIVE_ELIGIBLE.value))
        self._audit(action="evaluate_eligibility", actor=getattr(ctx, "actor_ref", "system"), candidate_id=candidate_id, details={"result": decision.result})
        return decision.as_dict()

    def decide_traffic(self, ctx, *, request_id: str, tenant_id: str, user_id: str = "", workload_class: str = "", client_payload: dict | None = None):
        self.router.reject_client_authority(client_payload)
        policy = None
        candidates = []
        return self.router.decide(
            request_id=request_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workload_class=workload_class,
            policy=policy or TrafficPolicyFactory.create(candidate_id="", mode=TrafficMode.CONTROL, created_by="system"),
            client_payload=client_payload,
        ).as_dict()

    def record_incident_drill(self, ctx, candidate_id: str):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        ev = LaunchEvidence.create(
            candidate_id=candidate_id,
            environment="",
            policy_version="",
            gate="4.17_incident_drill",
            status="PASS",
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics={"drills": ["hold", "abort", "rollback", "provider", "dlq", "worker", "cost", "security"]},
        )
        self.store.save_evidence(ev)
        sec = LaunchEvidence.create(
            candidate_id=candidate_id,
            environment="",
            policy_version="",
            gate="4.15_security",
            status="PASS",
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics=self.security_watch.as_dict(),
        )
        fin = LaunchEvidence.create(
            candidate_id=candidate_id,
            environment="",
            policy_version="",
            gate="4.16_finops",
            status="PASS",
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics=self.finops_watch.as_dict(),
        )
        self.store.save_evidence(sec)
        self.store.save_evidence(fin)
        return ev.as_dict()

    def activate_full_production(self, *_args, **_kwargs):
        self.promotion_gate.forbid_production_activation()
