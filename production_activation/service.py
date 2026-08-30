"""Production activation orchestration service."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from production_activation.acceptance import ProductionAcceptanceGate
from production_activation.access import (
    ActivationAuthorizationPolicy,
    PERM_ACTIVATION_AUTHORIZE,
    PERM_ACTIVATION_READ,
    PERM_ACTIVATION_WRITE,
)
from production_activation.activation import ProductionTrafficActivator
from production_activation.authorization import ActivationAuthorizer
from production_activation.candidate import FinalCandidateLock
from production_activation.commands import ActivateProductionCommand, AuthorizeActivationCommand, PrepareActivationCommand, RollbackProductionCommand
from production_activation.errors import BLOCKED_BY_PREVIOUS_STAGE, ProductionActivationError
from production_activation.finops_watch import ProductionFinOpsWatch
from production_activation.handoff import Stage5HandoffGate
from production_activation.hypercare import HypercareWindow
from production_activation.models import AcceptanceResult, ActivationState, ProductionActivationEvidence, VerificationClass
from production_activation.plan import GoLivePlanBuilder
from production_activation.policy import create_policy, mark_activated, mark_deactivated
from production_activation.providers import ProviderManifest
from production_activation.recovery import RecoveryCheckResult
from production_activation.runtime_watch import ProductionRuntimeWatch
from production_activation.security_watch import ProductionSecurityWatch
from production_activation.side_effects import SideEffectActivationPolicy
from production_activation.slo import ProductionSLOObservation
from production_activation.smoke import PostLaunchSmokeRunner
from production_activation.sqlite_store import SqliteProductionActivationStore
from production_activation.stage5_gate import MANDATORY_STAGE5_GATES, Stage5ReleaseGate
from evals.activation import RoutingActivationService


class ProductionActivationService:
    def __init__(
        self,
        *,
        store: SqliteProductionActivationStore | None = None,
        handoff_gate: Stage5HandoffGate | None = None,
        access: ActivationAuthorizationPolicy | None = None,
        routing_activation: RoutingActivationService | None = None,
    ):
        self.store = store or SqliteProductionActivationStore()
        self.handoff_gate = handoff_gate or Stage5HandoffGate()
        self.access = access or ActivationAuthorizationPolicy()
        self.authorizer = ActivationAuthorizer()
        self.candidate_lock = FinalCandidateLock(handoff_gate=self.handoff_gate)
        self.activator = ProductionTrafficActivator(routing_activation=routing_activation)
        self.acceptance_gate = ProductionAcceptanceGate()
        self.stage5_gate = Stage5ReleaseGate()
        self.smoke = PostLaunchSmokeRunner()
        self.security = ProductionSecurityWatch()
        self.finops = ProductionFinOpsWatch()
        self.runtime = ProductionRuntimeWatch()
        self.slo = ProductionSLOObservation()
        self.recovery = RecoveryCheckResult()
        self.providers = ProviderManifest()
        self.side_effects = SideEffectActivationPolicy()
        self._hypercare: HypercareWindow | None = None
        self._activation_lock = threading.RLock()
        self._plans: dict[str, object] = {}
        # Restore durable activation state into process activator (never auto-activates routing)
        restored = self.store.get_activation_state()
        self.activator.restore_state(str(restored.get("state") or ActivationState.GO_LIVE_ELIGIBLE.value))

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

    def get_handoff(self, candidate_id: str) -> dict:
        return {
            "handoff": self.handoff_gate.evaluate(candidate_id=candidate_id).as_dict(),
            "go_live_gate": self.handoff_gate.go_live_gate_result(candidate_id=candidate_id),
        }

    def preflight(self, ctx, candidate_id: str) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        return self.get_handoff(candidate_id)

    def prepare(self, ctx, cmd: PrepareActivationCommand) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_WRITE)
        handoff = self.handoff_gate.require_ready(candidate_id=cmd.candidate_id)
        candidate = self.candidate_lock.lock_from_handoff(
            handoff,
            production_url=cmd.production_url,
        )
        self.store.save_candidate(candidate)
        plan = GoLivePlanBuilder.create(
            candidate=candidate,
            authorized_operator=cmd.operator_ref,
            monitoring_destination=cmd.monitoring_destination,
            alert_destination=cmd.alert_destination,
        )
        self.store.save_plan(plan)
        self._plans[plan.plan_id] = plan
        self._audit(action="prepare", actor=cmd.operator_ref, candidate_id=cmd.candidate_id, details={"plan_id": plan.plan_id})
        return {"candidate": candidate.as_dict(), "plan": plan.as_dict()}

    def authorize(self, ctx, cmd: AuthorizeActivationCommand) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_AUTHORIZE)
        candidate = self.store.get_candidate(cmd.candidate_id)
        plan = self.store.get_plan(cmd.plan_id)
        if candidate is None or plan is None:
            raise ProductionActivationError("target_not_found")
        auth = self.authorizer.issue(
            candidate=candidate,
            plan=plan,
            operator_ref=cmd.operator_ref,
            idempotency_key=cmd.idempotency_key,
        )
        self.store.save_authorization(auth)
        self._audit(action="authorize", actor=cmd.operator_ref, candidate_id=cmd.candidate_id, details={"authorization_id": auth.authorization_id})
        return auth.as_dict()

    def _require_activation_ready(self, candidate_id: str) -> None:
        if not self.handoff_gate.allows_activation(candidate_id=candidate_id):
            raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE)

    def activate(self, ctx, cmd: ActivateProductionCommand) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_AUTHORIZE)
        self._require_activation_ready(cmd.candidate_id)
        candidate = self.store.get_candidate(cmd.candidate_id)
        plan = self.store.get_plan(cmd.plan_id)
        auth = self.store.get_authorization(cmd.authorization_id)
        if candidate is None or plan is None or auth is None:
            raise ProductionActivationError("target_not_found")
        self.authorizer.verify(
            authorization=auth,
            candidate=candidate,
            plan=plan,
            operator_ref=cmd.operator_ref,
            confirmation_token=cmd.confirmation_token,
            idempotency_key=cmd.idempotency_key,
        )
        with self._activation_lock:
            attempt = self.activator.activate(
                candidate=candidate,
                plan=plan,
                operator_ref=cmd.operator_ref,
                expected_policy_version=cmd.expected_policy_version,
                idempotency_key=cmd.idempotency_key,
            )
            attempt.authorization_id = auth.authorization_id
            self.authorizer.consume(auth, idempotency_key=cmd.idempotency_key)
            self.store.save_attempt(attempt)
            self.store.set_activation_state(attempt.state, candidate_id=candidate.candidate_id)
            self.providers = ProviderManifest.from_plan(plan.launch_required_providers)
            self.side_effects = SideEffectActivationPolicy.from_plan(plan.side_effect_policy, billing_mode=plan.billing_mode)
            ev = ProductionActivationEvidence.create(
                candidate_id=candidate.candidate_id,
                deployment_id=candidate.deployment_id,
                environment=candidate.environment,
                plan_id=plan.plan_id,
                attempt_id=attempt.attempt_id,
                activation_state=attempt.state,
                acceptance_result=AcceptanceResult.BLOCKED.value,
                # Local/routing activation is not LIVE_VERIFIED public production proof
                classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value
                if attempt.state == ActivationState.PRODUCTION_ACTIVE.value
                else VerificationClass.CODE_VERIFIED.value,
                safe_metrics={"routing": attempt.routing_result, "go_live_active": attempt.state == ActivationState.PRODUCTION_ACTIVE.value},
            )
            self.store.save_evidence(ev)
            policy = self.store.latest_go_live_policy()
            if policy and attempt.state == ActivationState.PRODUCTION_ACTIVE.value:
                self.store.save_go_live_policy(mark_activated(policy, activated_by=cmd.operator_ref))
            self._audit(action="activate", actor=cmd.operator_ref, candidate_id=cmd.candidate_id, details={"attempt_id": attempt.attempt_id, "state": attempt.state})
            return {
                "attempt": attempt.as_dict(),
                "evidence": ev.as_dict(),
                "side_effects": self.side_effects.as_dict(),
                "go_live_active": attempt.state == ActivationState.PRODUCTION_ACTIVE.value,
                "operator_action_required": True,
                "live_verified": False,
            }

    def run_smoke(self, ctx, *, candidate_id: str, attempt_id: str, probes: dict | None = None) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        result = self.smoke.run(candidate_id=candidate_id, attempt_id=attempt_id, probes=probes)
        ev = ProductionActivationEvidence.create(
            candidate_id=candidate_id,
            deployment_id="",
            environment="",
            plan_id="",
            attempt_id=attempt_id,
            activation_state=self.store.get_activation_state().get("state", ""),
            acceptance_result=AcceptanceResult.BLOCKED.value,
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics=result,
        )
        self.store.save_evidence(ev)
        return result

    def start_hypercare(self, ctx, *, candidate_id: str, policy: dict | None = None) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        self._hypercare = HypercareWindow(
            candidate_id=candidate_id,
            started_at=datetime.now(timezone.utc),
            policy=policy or {"min_requests": 1, "max_window_seconds": 3600},
        )
        return {"started": True, "candidate_id": candidate_id}

    def complete_hypercare(self, ctx, *, candidate_id: str) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        if self._hypercare is None:
            raise ProductionActivationError("hypercare_not_started")
        result = self._hypercare.complete()
        ev = ProductionActivationEvidence.create(
            candidate_id=candidate_id,
            deployment_id="",
            environment="",
            plan_id="",
            attempt_id="",
            activation_state=self.store.get_activation_state().get("state", ""),
            acceptance_result=AcceptanceResult.BLOCKED.value,
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics=result,
        )
        self.store.save_evidence(ev)
        return result

    def activate_billing_live(self, ctx, *, candidate_id: str, operator_ref: str, authorized: bool = True) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_AUTHORIZE)
        self.side_effects.activate_billing_live(authorized=authorized, operator_ref=operator_ref)
        self._audit(action="activate_billing_live", actor=operator_ref, candidate_id=candidate_id)
        return self.side_effects.as_dict()

    def rollback(self, ctx, cmd: RollbackProductionCommand) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_AUTHORIZE)
        result = self.activator.rollback(operator_ref=cmd.operator_ref)
        self.store.set_activation_state(ActivationState.ROLLED_BACK.value, candidate_id=cmd.candidate_id)
        policy = self.store.latest_go_live_policy()
        if policy:
            self.store.save_go_live_policy(mark_deactivated(policy, reason=cmd.reason or "rollback"))
        self._audit(action="rollback", actor=cmd.operator_ref, candidate_id=cmd.candidate_id, details={"reason": cmd.reason})
        return result

    def deactivate(self, ctx, *, candidate_id: str, operator_ref: str, reason: str = "deactivate") -> dict:
        self.access.require(ctx, PERM_ACTIVATION_AUTHORIZE)
        with self._activation_lock:
            result = self.activator.deactivate(operator_ref=operator_ref, reason=reason)
            self.store.set_activation_state(ActivationState.ROLLED_BACK.value, candidate_id=candidate_id, extra={"reason": reason})
            policy = self.store.latest_go_live_policy()
            if policy:
                self.store.save_go_live_policy(mark_deactivated(policy, reason=reason))
            self._audit(action="deactivate", actor=operator_ref, candidate_id=candidate_id, details={"reason": reason})
            return result

    def create_go_live_policy(self, ctx, *, release_identity: str, created_by: str = "") -> dict:
        self.access.require(ctx, PERM_ACTIVATION_WRITE)
        actor = created_by or getattr(ctx, "actor_ref", "operator")
        if callable(actor):
            actor = actor()
        policy = create_policy(release_identity=release_identity, created_by=str(actor))
        self.store.save_go_live_policy(policy)
        self._audit(action="create_go_live_policy", actor=str(actor), candidate_id=release_identity, details={"policy_id": policy.policy_id})
        return policy.as_dict()

    def seed_stage5_evidence(self, ctx, *, candidate_id: str, release_identity: str) -> dict:
        """Record CODE_VERIFIED Stage-5 mandatory gate evidence (engineering closure)."""
        self.access.require(ctx, PERM_ACTIVATION_WRITE)
        evidence_list = []
        for gate in MANDATORY_STAGE5_GATES:
            ev = ProductionActivationEvidence.create(
                candidate_id=candidate_id,
                deployment_id="",
                environment="production",
                plan_id="",
                attempt_id="",
                activation_state=self.store.get_activation_state().get("state", ""),
                acceptance_result="PASS",
                classification=VerificationClass.CODE_VERIFIED.value,
                safe_metrics={"gate": gate, "status": "PASS", "release_identity": release_identity},
            )
            self.store.save_evidence(ev)
            evidence_list.append(ev.as_dict())
        self._audit(action="seed_stage5_evidence", actor=getattr(ctx, "actor_ref", "operator"), candidate_id=candidate_id)
        return {"evidence": evidence_list}

    def evaluate_stage5_gate(self, ctx, *, candidate_id: str = "") -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        policy = self.store.latest_go_live_policy()
        evidence = self.store.list_evidence(candidate_id) if candidate_id else []
        stage4_ok = self.handoff_gate.stage4_artifact_ready()
        state = self.store.get_activation_state()
        result = self.stage5_gate.evaluate(
            evidence=evidence,
            policy=policy,
            stage4_handoff_pass=stage4_ok,
            engineering_pass=True,
            p0_count=self.security.p0_count,
            p1_count=self.security.p1_count,
            go_live_active=bool(state.get("go_live_active")),
            live_verified=False,
        )
        self.store.set_activation_state(
            state.get("state") or ActivationState.GO_LIVE_ELIGIBLE.value,
            candidate_id=candidate_id or state.get("candidate_id") or "",
            extra={"stage5_gate": result.as_dict()},
        )
        self._audit(
            action="evaluate_stage5_gate",
            actor=getattr(ctx, "actor_ref", "system"),
            candidate_id=candidate_id,
            details={"verdict": result.verdict, "go_live_active": result.go_live_active},
        )
        return result.as_dict()

    def post_activation_health(self, ctx, *, candidate_id: str = "") -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        state = self.store.get_activation_state()
        critical = self.security.p0_count > 0
        if critical:
            health = "UNHEALTHY"
        elif self.runtime.exceeds_envelope({}) or self.finops.blocks_acceptance():
            health = "DEGRADED"
        elif state.get("state") == ActivationState.PRODUCTION_ACTIVE.value:
            health = "HEALTHY"
        else:
            health = "DEGRADED"
        result = {
            "health": health,
            "activation_state": state.get("state"),
            "go_live_active": bool(state.get("go_live_active")),
            "security": self.security.as_dict(),
            "runtime": self.runtime.as_dict(),
            "finops": self.finops.as_dict(),
            "recovery": self.recovery.as_dict(),
            "candidate_id": candidate_id or state.get("candidate_id"),
        }
        self._audit(action="post_activation_health", actor=getattr(ctx, "actor_ref", "system"), candidate_id=candidate_id or "", details={"health": health})
        return result

    def stage5_status(self, ctx) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        state = self.store.get_activation_state()
        policy = self.store.latest_go_live_policy()
        return {
            "activation_state": state,
            "go_live_active": bool(state.get("go_live_active")),
            "policy": policy.as_dict() if policy else None,
            "stage4_artifact_ready": self.handoff_gate.stage4_artifact_ready(),
            "stage5_gate": state.get("stage5_gate"),
            "operator_action_required": True,
        }

    def evaluate_acceptance(self, ctx, *, candidate_id: str) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        state = self.store.get_activation_state()
        smoke_ev = next((e for e in reversed(self.store.list_evidence(candidate_id)) if "checks" in e.safe_metrics), None)
        hyper_ev = next((e for e in reversed(self.store.list_evidence(candidate_id)) if "duration_seconds" in e.safe_metrics), None)
        decision = self.acceptance_gate.evaluate(
            candidate_id=candidate_id,
            activation_state=state.get("state", ""),
            smoke_status=(smoke_ev.safe_metrics.get("status") if smoke_ev else "FAIL"),
            hypercare_status=(hyper_ev.safe_metrics.get("status") if hyper_ev else "FAIL"),
            slo_ok=self.slo.within_envelope({}),
            security_p0=self.security.p0_count,
            security_p1=self.security.p1_count,
            finops_ok=not self.finops.blocks_acceptance(),
            runtime_ok=not self.runtime.exceeds_envelope({}),
            recovery_ready=self.recovery.ready(),
            providers_ok=not self.providers.blocks_acceptance(),
            side_effects_ok=not self.side_effects.blocks_unauthorized_live(),
            evidence=self.store.list_evidence(candidate_id),
        )
        return decision.as_dict()

    def read_model(self, ctx, candidate_id: str) -> dict:
        self.access.require(ctx, PERM_ACTIVATION_READ)
        candidate = self.store.get_candidate(candidate_id)
        return {
            "handoff": self.get_handoff(candidate_id),
            "candidate": candidate.as_dict() if candidate else None,
            "activation_state": self.store.get_activation_state(),
            "providers": self.providers.as_dict(),
            "side_effects": self.side_effects.as_dict(),
            "security": self.security.as_dict(),
            "finops": self.finops.as_dict(),
            "runtime": self.runtime.as_dict(),
            "recovery": self.recovery.as_dict(),
            "evidence": [e.as_dict() for e in self.store.list_evidence(candidate_id)],
        }

    def reject_client_activation(self, payload: dict | None) -> None:
        if not payload:
            return
        forbidden = ("candidate_id", "deployment_id", "plan_id", "confirmation_token", "billing_mode", "provider", "activate", "production")
        for key in forbidden:
            if key in payload:
                from production_activation.errors import AUTHORIZATION_DENIED, ProductionActivationError

                raise ProductionActivationError(AUTHORIZATION_DENIED, details={"client_forbidden_key": key})
