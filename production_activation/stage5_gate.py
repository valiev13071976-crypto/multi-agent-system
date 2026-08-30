"""Stage-5 pre-activation / release gate — GO_LIVE_READY vs ACTIVE."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from production_activation.errors import GO_LIVE_BLOCKED, ProductionActivationError
from production_activation.models import ProductionActivationEvidence
from production_activation.policy import GoLivePolicy


MANDATORY_STAGE5_GATES = (
    "5.1_stage4_handoff",
    "5.2_release_identity",
    "5.3_deployment_health",
    "5.4_security",
    "5.5_tenant_isolation",
    "5.6_runtime_protection",
    "5.7_provider_governance",
    "5.8_budget_guard",
    "5.9_observability",
    "5.10_alerting",
    "5.11_backup_recovery",
    "5.12_rollback_readiness",
    "5.13_operator_authorization",
    "5.14_activation_policy",
    "5.15_final_activation_gate",
)

INFORMATIONAL_STAGE5_GATES = (
    "5.16_post_activation_live",
)


@dataclass
class Stage5GateResult:
    verdict: str
    engineering: str
    stage4_handoff: str
    release_readiness: str
    go_live_eligible: bool
    go_live_active: bool
    operator_action_required: bool
    gates: dict[str, str] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)
    release_identity: str = ""
    evidence_id: str = ""
    policy_version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "engineering": self.engineering,
            "stage4_handoff": self.stage4_handoff,
            "release_readiness": self.release_readiness,
            "go_live_eligible": self.go_live_eligible,
            "go_live_active": self.go_live_active,
            "operator_action_required": self.operator_action_required,
            "gates": dict(self.gates),
            "blocked": list(self.blocked),
            "release_identity": self.release_identity,
            "evidence_id": self.evidence_id,
            "policy_version": self.policy_version,
        }


def _evidence_gate_status(evidence: list[ProductionActivationEvidence], gate: str) -> str:
    for e in evidence:
        metrics = dict(e.safe_metrics or {})
        if metrics.get("gate") == gate:
            return str(metrics.get("status") or "MISSING")
    return "MISSING"


class Stage5ReleaseGate:
    """Fail-closed Stage-5 evaluator. Evaluation never activates GO LIVE."""

    def evaluate(
        self,
        *,
        evidence: list[ProductionActivationEvidence],
        policy: GoLivePolicy | None = None,
        stage4_handoff_pass: bool = False,
        engineering_pass: bool = True,
        p0_count: int = 0,
        p1_count: int = 0,
        go_live_active: bool = False,
        live_verified: bool = False,
    ) -> Stage5GateResult:
        gates = {g: _evidence_gate_status(evidence, g) for g in MANDATORY_STAGE5_GATES}
        for g in INFORMATIONAL_STAGE5_GATES:
            status = _evidence_gate_status(evidence, g)
            if status != "MISSING":
                gates[g] = status

        if stage4_handoff_pass and gates.get("5.1_stage4_handoff") != "PASS":
            # Authoritative Stage-4 artifact may satisfy 5.1 without duplicate evidence row
            gates["5.1_stage4_handoff"] = "PASS"

        blocked = [g for g, status in gates.items() if g in MANDATORY_STAGE5_GATES and status != "PASS"]
        if not stage4_handoff_pass and gates.get("5.1_stage4_handoff") != "PASS":
            if "5.1_stage4_handoff" not in blocked:
                blocked.append("5.1_stage4_handoff")
            gates["5.1_stage4_handoff"] = "FAIL"

        if p0_count or p1_count:
            blocked.append("open_p0_p1")
        engineering = "PASS" if engineering_pass else "FAIL"
        if engineering == "FAIL":
            blocked.append("engineering")

        release_identity = policy.release_identity if policy else ""
        policy_version = policy.policy_version if policy else ""
        active = bool(go_live_active or (policy.go_live_active if policy else False))
        blocked = list(dict.fromkeys(blocked))

        if blocked:
            return Stage5GateResult(
                verdict="GO_LIVE_BLOCKED",
                engineering=engineering,
                stage4_handoff="PASS" if stage4_handoff_pass or gates.get("5.1_stage4_handoff") == "PASS" else "FAIL",
                release_readiness="NOT_READY",
                go_live_eligible=False,
                go_live_active=False,
                operator_action_required=True,
                gates=gates,
                blocked=blocked,
                release_identity=release_identity,
                policy_version=policy_version,
            )

        if active and live_verified:
            return Stage5GateResult(
                verdict="GO_LIVE_PASS",
                engineering=engineering,
                stage4_handoff="PASS",
                release_readiness="READY",
                go_live_eligible=True,
                go_live_active=True,
                operator_action_required=False,
                gates=gates,
                blocked=[],
                release_identity=release_identity,
                policy_version=policy_version,
                evidence_id=evidence[-1].evidence_id if evidence else "",
            )

        return Stage5GateResult(
            verdict="GO_LIVE_READY",
            engineering=engineering,
            stage4_handoff="PASS",
            release_readiness="READY",
            go_live_eligible=True,
            go_live_active=False,
            operator_action_required=True,
            gates=gates,
            blocked=[],
            release_identity=release_identity,
            policy_version=policy_version,
            evidence_id=evidence[-1].evidence_id if evidence else "",
        )

    def require_ready(self, result: Stage5GateResult) -> Stage5GateResult:
        if result.verdict not in {"GO_LIVE_READY", "GO_LIVE_PASS"}:
            raise ProductionActivationError(GO_LIVE_BLOCKED, details=result.as_dict())
        return result
