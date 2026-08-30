"""GO_LIVE eligibility gate — non-mutating."""

from __future__ import annotations

from controlled_launch.errors import GO_LIVE_BLOCKED, PRODUCTION_ACTIVE_FORBIDDEN, ControlledLaunchError
from controlled_launch.models import CandidateStatus, LaunchEvidence, PromotionDecision, PromotionResult, RolloutStep, ShadowGateResult


class PromotionGate:
    """Evaluates eligibility only; does not activate production."""

    REQUIRED_GATES = (
        "4.3_internal",
        "4.5_shadow_gate",
        "4.8_canary_observation",
        "4.10_rollout",
        "4.15_security",
        "4.16_finops",
        "4.17_incident_drill",
    )

    def evaluate(
        self,
        *,
        candidate_id: str,
        candidate_status: str,
        evidence: list[LaunchEvidence],
        p0_count: int = 0,
        p1_count: int = 0,
        stage3_ready: bool = False,
        rollout_step: str = "",
    ) -> PromotionDecision:
        if p0_count > 0 or p1_count > 0:
            return PromotionDecision(
                candidate_id=candidate_id,
                result=PromotionResult.GO_LIVE_BLOCKED.value,
                reason="open_p0_p1",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if not stage3_ready:
            return PromotionDecision(
                candidate_id=candidate_id,
                result=PromotionResult.GO_LIVE_BLOCKED.value,
                reason="stage3_not_ready",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        by_gate = {e.gate: e for e in evidence}
        shadow = by_gate.get("4.5_shadow_gate")
        if shadow is None or shadow.status != ShadowGateResult.SHADOW_PASS.value:
            return PromotionDecision(
                candidate_id=candidate_id,
                result=PromotionResult.GO_LIVE_BLOCKED.value,
                reason="shadow_gate_not_pass",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        missing = [g for g in self.REQUIRED_GATES if g not in by_gate or by_gate[g].status not in {"PASS", ShadowGateResult.SHADOW_PASS.value}]
        if missing:
            return PromotionDecision(
                candidate_id=candidate_id,
                result=PromotionResult.GO_LIVE_BLOCKED.value,
                reason=f"missing_evidence:{','.join(missing)}",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if rollout_step != RolloutStep.GO_LIVE_ELIGIBLE.value and candidate_status != CandidateStatus.GO_LIVE_ELIGIBLE.value:
            return PromotionDecision(
                candidate_id=candidate_id,
                result=PromotionResult.GO_LIVE_BLOCKED.value,
                reason="rollout_incomplete",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        return PromotionDecision(
            candidate_id=candidate_id,
            result=PromotionResult.GO_LIVE_ELIGIBLE.value,
            reason="all_gates_pass",
            evidence_ids=tuple(e.evidence_id for e in evidence),
        )

    @staticmethod
    def forbid_production_activation() -> None:
        raise ControlledLaunchError(
            PRODUCTION_ACTIVE_FORBIDDEN,
            message="Stage 4 cannot activate PRODUCTION_ACTIVE; Stage 5 owns explicit GO LIVE",
        )
