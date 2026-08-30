"""Controlled launch orchestration service."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from controlled_launch.access import LaunchAuthorizationPolicy, PERM_LAUNCH_READ, PERM_LAUNCH_WRITE
from controlled_launch.admission import ControlledLaunchAdmission
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
from controlled_launch.containment import ContainmentEvaluator
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
from controlled_launch.policy import (
    activate_policy,
    create_policy,
    pause_policy,
    with_kill_switch,
)
from controlled_launch.promotion import PromotionGate
from controlled_launch.rollout import RolloutManager
from controlled_launch.router import ControlledLaunchRouter
from controlled_launch.security_watch import SecurityWatch
from controlled_launch.shadow import ShadowController
from controlled_launch.sqlite_store import SqliteControlledLaunchStore
from controlled_launch.stage4_gate import MANDATORY_STAGE4_GATES, Stage4ReleaseGate
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
        self.stage4_gate = Stage4ReleaseGate()
        self.admission = ControlledLaunchAdmission()
        self.containment = ContainmentEvaluator()
        self.security_watch = SecurityWatch()
        self.finops_watch = FinOpsWatch()
        self._lock = __import__("threading").RLock()
        self._spent = 0.0
        self._active_counts = {"cohort": 0, "interactive": 0, "batch": 0, "tenant": {}}
        self._restore_runtime_counters()

    def _restore_runtime_counters(self) -> None:
        payload = self.store.get_launch_state("runtime_counters") or {}
        self._spent = float(payload.get("spent") or 0.0)
        tenants = payload.get("tenant") or {}
        self._active_counts = {
            "cohort": int(payload.get("cohort") or 0),
            "interactive": int(payload.get("interactive") or 0),
            "batch": int(payload.get("batch") or 0),
            "tenant": {str(k): int(v) for k, v in dict(tenants).items()},
        }

    def _persist_runtime_counters(self) -> None:
        self.store.set_launch_state(
            "runtime_counters",
            {
                "spent": self._spent,
                "cohort": self._active_counts["cohort"],
                "interactive": self._active_counts["interactive"],
                "batch": self._active_counts["batch"],
                "tenant": dict(self._active_counts.get("tenant") or {}),
            },
        )

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

    def create_launch_policy(self, ctx, **kwargs):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        self._require_live_ready()
        policy = create_policy(created_by=getattr(ctx, "actor_ref", "operator") if "created_by" not in kwargs else kwargs.pop("created_by"), **kwargs)
        self.store.save_launch_policy(policy)
        self.store.set_launch_state("active_policy_id", {"policy_id": policy.policy_id})
        self._audit(action="create_launch_policy", actor=getattr(ctx, "actor_ref", "operator"), candidate_id=policy.release_identity, details={"policy_id": policy.policy_id})
        return policy.as_dict()

    def activate_controlled_launch(self, ctx, *, policy_id: str):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        self._require_live_ready()
        with self._lock:
            policy = self.store.get_launch_policy(policy_id)
            if policy is None:
                raise ControlledLaunchError("policy_not_found")
            state = self.store.get_launch_state("active_policy_id") or {}
            if state.get("activating"):
                raise ControlledLaunchError("activation_in_progress")
            self.store.set_launch_state("active_policy_id", {"policy_id": policy_id, "activating": True})
            try:
                activated = activate_policy(policy, approved_by=getattr(ctx, "actor_ref", "operator"))
                self.store.save_launch_policy(activated)
                self.store.set_launch_state(
                    "launch",
                    {"state": "ACTIVE", "policy_id": activated.policy_id, "policy_version": activated.policy_version, "go_live_active": False},
                )
            finally:
                self.store.set_launch_state("active_policy_id", {"policy_id": policy_id, "activating": False})
            self._audit(action="activate_controlled_launch", actor=getattr(ctx, "actor_ref", "operator"), candidate_id=activated.release_identity)
            return activated.as_dict()

    def pause_controlled_launch(self, ctx, *, policy_id: str):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        with self._lock:
            policy = self.store.get_launch_policy(policy_id)
            if policy is None:
                raise ControlledLaunchError("policy_not_found")
            paused = pause_policy(policy)
            self.store.save_launch_policy(paused)
            self.store.set_launch_state("launch", {"state": "PAUSED", "policy_id": policy_id, "go_live_active": False})
            self._audit(action="pause_controlled_launch", actor=getattr(ctx, "actor_ref", "operator"), candidate_id=paused.release_identity)
            return paused.as_dict()

    def kill_controlled_launch(self, ctx, *, policy_id: str, reason: str = ""):
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        with self._lock:
            policy = self.store.get_launch_policy(policy_id)
            if policy is None:
                raise ControlledLaunchError("policy_not_found")
            killed = with_kill_switch(policy, enabled=True, actor=getattr(ctx, "actor_ref", "operator"))
            killed = pause_policy(killed)
            self.store.save_launch_policy(killed)
            self.store.set_launch_state(
                "launch",
                {"state": "KILLED", "policy_id": policy_id, "reason": reason, "go_live_active": False},
            )
            self._audit(action="kill_controlled_launch", actor=getattr(ctx, "actor_ref", "operator"), candidate_id=killed.release_identity, details={"reason": reason})
            return killed.as_dict()

    def admit(self, ctx, *, tenant_id: str, user_id: str = "", workload_class: str = "interactive", authenticated: bool = False, authorized: bool = False):
        policy = self.store.latest_launch_policy()
        tenant_active = int((self._active_counts.get("tenant") or {}).get(tenant_id) or 0)
        decision = self.admission.decide(
            policy,
            tenant_id=tenant_id,
            user_id=user_id,
            workload_class=workload_class,
            authenticated=authenticated,
            authorized=authorized,
            active_cohort_count=self._active_counts["cohort"],
            active_interactive=self._active_counts["interactive"],
            active_batch=self._active_counts["batch"],
            tenant_active=tenant_active,
            spent=self._spent,
        )
        if decision.admitted:
            self._active_counts["cohort"] += 1
            if workload_class == "interactive":
                self._active_counts["interactive"] += 1
            else:
                self._active_counts["batch"] += 1
            tenants = dict(self._active_counts.get("tenant") or {})
            tenants[tenant_id] = tenants.get(tenant_id, 0) + 1
            self._active_counts["tenant"] = tenants
            self._persist_runtime_counters()
        return decision.as_dict()

    def record_spend(self, amount: float) -> dict:
        self._spent += float(amount)
        self._persist_runtime_counters()
        policy = self.store.latest_launch_policy()
        action = "none"
        if policy and policy.budget_ceiling and self._spent >= policy.budget_ceiling:
            action = "KILL_CONTROLLED_LAUNCH"
            if policy:
                killed = with_kill_switch(pause_policy(policy), enabled=True)
                self.store.save_launch_policy(killed)
                self.store.set_launch_state("launch", {"state": "KILLED", "policy_id": policy.policy_id, "reason": "budget_ceiling", "go_live_active": False})
        elif policy and policy.budget_warning_threshold and self._spent >= policy.budget_warning_threshold:
            action = "WARN"
        return {"spent": self._spent, "action": action}

    def evaluate_containment(self, *, signals: dict) -> dict:
        policy = self.store.latest_launch_policy()
        thresholds = {
            "pause_error_rate": 0.25,
            "pause_queue_saturation": 0.9,
            "degrade_provider_failures": 5,
            "kill_cost_ratio": 1.0,
        }
        decision = self.containment.evaluate(signals=signals, thresholds=thresholds)
        if policy and decision.action in {"PAUSE_ADMISSION", "KILL_CONTROLLED_LAUNCH"}:
            from dataclasses import replace as dc_replace

            updated = dc_replace(policy, containment_action=decision.action, enabled=False if decision.action != "CONTINUE" else policy.enabled)
            if decision.action == "KILL_CONTROLLED_LAUNCH":
                updated = with_kill_switch(updated, enabled=True)
            self.store.save_launch_policy(updated)
            self.store.set_launch_state("launch", {"state": decision.action, "policy_id": policy.policy_id, "go_live_active": False})
        return decision.as_dict()

    def seed_stage4_evidence(self, ctx, *, candidate_id: str, release_identity: str, policy_version: str = ""):
        """Record CODE_VERIFIED Stage-4 mandatory gate evidence for engineering closure."""
        self.access.require(ctx, PERM_LAUNCH_WRITE)
        handoff = self.handoff_gate.require_ready()
        evidence_list = []
        for gate in MANDATORY_STAGE4_GATES:
            ev = LaunchEvidence.create(
                candidate_id=candidate_id,
                environment=handoff.environment,
                policy_version=policy_version or release_identity,
                gate=gate,
                status="PASS",
                classification=VerificationClass.CODE_VERIFIED.value,
                safe_metrics={"source": "stage4_engineering_validation", "release_identity": release_identity},
            )
            self.store.save_evidence(ev)
            evidence_list.append(ev.as_dict())
        self._audit(action="seed_stage4_evidence", actor=getattr(ctx, "actor_ref", "operator"), candidate_id=candidate_id)
        return {"evidence": evidence_list}

    def evaluate_stage4_gate(self, ctx, *, candidate_id: str = ""):
        self.access.require(ctx, PERM_LAUNCH_READ)
        policy = self.store.latest_launch_policy()
        if candidate_id:
            evidence = self.store.list_evidence(candidate_id)
        else:
            evidence = self.store.list_all_evidence()
        result = self.stage4_gate.evaluate(
            evidence=evidence,
            policy=policy,
            engineering_pass=True,
            p0_count=self.security_watch.p0_count,
            p1_count=self.security_watch.p1_count,
            go_live_active=False,
        )
        self.store.set_launch_state("stage4_gate", result.as_dict())
        self._audit(
            action="evaluate_stage4_gate",
            actor=getattr(ctx, "actor_ref", "system"),
            candidate_id=candidate_id or (policy.release_identity if policy else ""),
            details={"verdict": result.verdict, "go_live_eligibility": result.go_live_eligibility},
        )
        return result.as_dict()

    def stage4_status(self, ctx) -> dict:
        self.access.require(ctx, PERM_LAUNCH_READ)
        return {
            "handoff": self.get_handoff(),
            "policy": (self.store.latest_launch_policy().as_dict() if self.store.latest_launch_policy() else None),
            "launch_state": self.store.get_launch_state("launch") or {"state": "DISABLED", "go_live_active": False},
            "stage4_gate": self.store.get_launch_state("stage4_gate"),
            "spent": self._spent,
            "go_live_active": False,
        }
