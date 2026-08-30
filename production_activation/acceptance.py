"""Production acceptance decision — non-mutating."""

from __future__ import annotations

from production_activation.models import AcceptanceResult, ActivationState, ProductionAcceptanceDecision, ProductionActivationEvidence


class ProductionAcceptanceGate:
    """Evaluates acceptance only; does not mutate routing."""

    def evaluate(
        self,
        *,
        candidate_id: str,
        activation_state: str,
        smoke_status: str,
        hypercare_status: str,
        slo_ok: bool,
        security_p0: int,
        security_p1: int,
        finops_ok: bool,
        runtime_ok: bool,
        recovery_ready: bool,
        providers_ok: bool,
        side_effects_ok: bool,
        evidence: list[ProductionActivationEvidence],
    ) -> ProductionAcceptanceDecision:
        if activation_state == ActivationState.ROLLED_BACK.value:
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.ROLLED_BACK.value,
                reason="rolled_back",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if activation_state != ActivationState.PRODUCTION_ACTIVE.value:
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.BLOCKED.value,
                reason="not_production_active",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if smoke_status != "PASS":
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.PRODUCTION_UNSTABLE.value,
                reason="smoke_fail",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if security_p0 > 0:
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.PRODUCTION_UNSTABLE.value,
                reason="security_p0",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if security_p1 > 0 or hypercare_status != "PASS" or not slo_ok or not finops_ok or not runtime_ok or not recovery_ready or not providers_ok or not side_effects_ok:
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.PRODUCTION_UNSTABLE.value,
                reason="hypercare_or_watch_fail",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        return ProductionAcceptanceDecision(
            candidate_id=candidate_id,
            result=AcceptanceResult.PRODUCTION_ACCEPTED.value,
            reason="all_criteria_pass",
            evidence_ids=tuple(e.evidence_id for e in evidence),
        )
