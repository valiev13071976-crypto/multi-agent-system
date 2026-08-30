"""Stage-4 release gate — CONTROLLED_LAUNCH_PASS / GO_LIVE_ELIGIBLE / not ACTIVE."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from controlled_launch.errors import PRODUCTION_ACTIVE_FORBIDDEN, ControlledLaunchError
from controlled_launch.models import LaunchEvidence, VerificationClass
from controlled_launch.policy import ControlledLaunchPolicy


MANDATORY_STAGE4_GATES = (
    "4.1_stage3_prerequisite",
    "4.2_policy",
    "4.3_cohort_admission",
    "4.4_capacity_bounds",
    "4.5_budget_guard",
    "4.6_security_auth",
    "4.7_tenant_isolation",
    "4.8_capability_restrictions",
    "4.9_observability",
    "4.11_kill_switch",
    "4.12_containment",
    "4.13_rollback",
    "4.15_go_live_gate",
)

INFORMATIONAL_STAGE4_GATES = (
    "4.10_alerting",
    "4.14_controlled_traffic",
)


@dataclass
class Stage4GateResult:
    verdict: str
    engineering: str
    controlled_launch: str
    go_live_eligibility: str
    go_live_active: bool
    gates: dict[str, str] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)
    release_identity: str = ""
    evidence_id: str = ""
    policy_version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "engineering": self.engineering,
            "controlled_launch": self.controlled_launch,
            "go_live_eligibility": self.go_live_eligibility,
            "go_live_active": self.go_live_active,
            "gates": dict(self.gates),
            "blocked": list(self.blocked),
            "release_identity": self.release_identity,
            "evidence_id": self.evidence_id,
            "policy_version": self.policy_version,
        }


class Stage4ReleaseGate:
    """Fail-closed Stage-4 evaluator. Never activates Stage 5."""

    def evaluate(
        self,
        *,
        evidence: list[LaunchEvidence],
        policy: ControlledLaunchPolicy | None = None,
        engineering_pass: bool = True,
        p0_count: int = 0,
        p1_count: int = 0,
        go_live_active: bool = False,
    ) -> Stage4GateResult:
        if go_live_active:
            raise ControlledLaunchError(
                PRODUCTION_ACTIVE_FORBIDDEN,
                message="Stage 4 must never report GO_LIVE_ACTIVE=true",
            )
        by_gate = {e.gate: e for e in evidence}
        gates = {g: (by_gate[g].status if g in by_gate else "MISSING") for g in MANDATORY_STAGE4_GATES}
        for g in INFORMATIONAL_STAGE4_GATES:
            if g in by_gate:
                gates[g] = by_gate[g].status
        blocked = [g for g, status in gates.items() if g in MANDATORY_STAGE4_GATES and status not in {"PASS", "SHADOW_PASS"}]
        if p0_count or p1_count:
            blocked.append("open_p0_p1")
        engineering = "PASS" if engineering_pass else "FAIL"
        if engineering == "FAIL":
            blocked.append("engineering")
        release_identity = policy.release_identity if policy else ""
        policy_version = policy.policy_version if policy else ""
        if blocked:
            return Stage4GateResult(
                verdict="CONTROLLED_LAUNCH_BLOCKED",
                engineering=engineering,
                controlled_launch="BLOCKED",
                go_live_eligibility="NOT_ELIGIBLE",
                go_live_active=False,
                gates=gates,
                blocked=blocked,
                release_identity=release_identity,
                policy_version=policy_version,
            )
        return Stage4GateResult(
            verdict="CONTROLLED_LAUNCH_PASS",
            engineering=engineering,
            controlled_launch="PASS",
            go_live_eligibility="ELIGIBLE",
            go_live_active=False,
            gates=gates,
            blocked=[],
            release_identity=release_identity,
            policy_version=policy_version,
            evidence_id=evidence[-1].evidence_id if evidence else "",
        )

    @staticmethod
    def record_gate(
        *,
        candidate_id: str,
        environment: str,
        policy_version: str,
        gate: str,
        status: str,
        classification: str = VerificationClass.CODE_VERIFIED.value,
        safe_metrics: dict | None = None,
    ) -> LaunchEvidence:
        return LaunchEvidence.create(
            candidate_id=candidate_id,
            environment=environment,
            policy_version=policy_version,
            gate=gate,
            status=status,
            classification=classification,
            safe_metrics=safe_metrics,
        )
