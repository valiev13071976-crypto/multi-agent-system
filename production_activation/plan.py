"""GoLive plan creation and validation."""

from __future__ import annotations

import hashlib
import uuid

from production_activation.errors import PLAN_INCOMPLETE, ProductionActivationError
from production_activation.models import FinalProductionCandidate, GoLivePlan


def plan_fingerprint(plan: GoLivePlan) -> str:
    raw = "|".join([plan.plan_id, plan.candidate_id, plan.environment, plan.rollback_target, plan.billing_mode])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class GoLivePlanBuilder:
    REQUIRED_SMOKE = ("health", "readiness", "auth", "tenant", "chat", "ai", "workflow", "persistence", "admin")

    @classmethod
    def create(
        cls,
        *,
        candidate: FinalProductionCandidate,
        authorized_operator: str,
        activation_window: str = "approved",
        traffic_transition: str = "full_production",
        required_providers: tuple[str, ...] = ("openai",),
        billing_mode: str = "sandbox",
        side_effect_policy: dict[str, str] | None = None,
        monitoring_destination: str = "",
        alert_destination: str = "",
        incident_owner: str = "",
    ) -> GoLivePlan:
        if not candidate.rollback_target.strip():
            raise ProductionActivationError(PLAN_INCOMPLETE, details={"rollback_target": "missing"})
        if not monitoring_destination.strip():
            raise ProductionActivationError(PLAN_INCOMPLETE, details={"monitoring_destination": "missing"})
        if not alert_destination.strip():
            raise ProductionActivationError(PLAN_INCOMPLETE, details={"alert_destination": "missing"})
        plan = GoLivePlan(
            plan_id=f"glp-{uuid.uuid4().hex[:12]}",
            candidate_id=candidate.candidate_id,
            environment=candidate.environment,
            activation_window=activation_window,
            authorized_operator=authorized_operator,
            traffic_transition=traffic_transition,
            launch_required_providers=required_providers,
            billing_mode=billing_mode,
            side_effect_policy=dict(side_effect_policy or {"billing": "sandbox", "email": "disabled", "telegram": "disabled"}),
            expected_capacity=dict(candidate.capacity_envelope or {"max_rps": 100}),
            cost_envelope=dict(candidate.cost_envelope or {"max_hourly": 50.0}),
            smoke_plan=cls.REQUIRED_SMOKE,
            hypercare_policy={"min_requests": 10, "max_window_seconds": 3600, "guardrails": {}},
            abort_conditions=("critical_smoke_fail", "p0_security", "runaway_cost"),
            rollback_conditions=("p0_incident", "activation_failure"),
            rollback_target=candidate.rollback_target,
            monitoring_destination=monitoring_destination,
            alert_destination=alert_destination,
            backup_state=candidate.backup_state,
            incident_owner=incident_owner or authorized_operator,
            status="READY",
        )
        fp = plan_fingerprint(plan)
        object.__setattr__(plan, "fingerprint", fp)
        return plan
