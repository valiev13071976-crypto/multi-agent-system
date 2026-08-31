"""Production acceptance decision — non-mutating."""

from __future__ import annotations

from production_activation.acceptance_evidence import _is_hypercare, _is_live_smoke
from production_activation.models import (
    AcceptanceResult,
    ActivationState,
    ProductionAcceptanceDecision,
    ProductionActivationEvidence,
    VerificationClass,
)


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
        require_live_evidence: bool = False,
        plan_id: str = "",
        release_identity: str = "",
        smoke_classification: str = "",
        hypercare_classification: str = "",
        live_block_reason: str = "",
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

        if require_live_evidence:
            if live_block_reason:
                return ProductionAcceptanceDecision(
                    candidate_id=candidate_id,
                    result=AcceptanceResult.BLOCKED.value,
                    reason=live_block_reason,
                    evidence_ids=tuple(e.evidence_id for e in evidence),
                )
            smoke_ok = (
                smoke_status == "PASS"
                and smoke_classification == VerificationClass.LIVE_VERIFIED.value
            )
            hyper_ok = (
                hypercare_status == "PASS"
                and hypercare_classification == VerificationClass.LIVE_VERIFIED.value
            )
            if plan_id or release_identity:
                bound_smoke = False
                bound_hyper = False
                for e in evidence:
                    m = e.safe_metrics or {}
                    if _is_live_smoke(e) and e.classification == VerificationClass.LIVE_VERIFIED.value and m.get("status") == "PASS":
                        if plan_id and str(e.plan_id or m.get("plan_id") or "") != plan_id:
                            continue
                        if release_identity and str(m.get("release_identity") or "") != release_identity:
                            continue
                        bound_smoke = True
                    if (
                        _is_hypercare(e)
                        and e.classification == VerificationClass.LIVE_VERIFIED.value
                        and m.get("status") == "PASS"
                    ):
                        if plan_id and str(e.plan_id or m.get("plan_id") or "") != plan_id:
                            continue
                        if release_identity and str(m.get("release_identity") or "") != release_identity:
                            continue
                        bound_hyper = True
                if not bound_smoke:
                    return ProductionAcceptanceDecision(
                        candidate_id=candidate_id,
                        result=AcceptanceResult.BLOCKED.value,
                        reason="live_smoke_missing_or_unbound",
                        evidence_ids=tuple(e.evidence_id for e in evidence),
                    )
                if not bound_hyper:
                    return ProductionAcceptanceDecision(
                        candidate_id=candidate_id,
                        result=AcceptanceResult.BLOCKED.value,
                        reason="live_hypercare_missing_or_unbound",
                        evidence_ids=tuple(e.evidence_id for e in evidence),
                    )
            elif not smoke_ok:
                return ProductionAcceptanceDecision(
                    candidate_id=candidate_id,
                    result=AcceptanceResult.BLOCKED.value,
                    reason="live_smoke_required",
                    evidence_ids=tuple(e.evidence_id for e in evidence),
                )
            elif not hyper_ok:
                return ProductionAcceptanceDecision(
                    candidate_id=candidate_id,
                    result=AcceptanceResult.BLOCKED.value,
                    reason="hypercare_not_live_verified",
                    evidence_ids=tuple(e.evidence_id for e in evidence),
                )
            if not recovery_ready:
                return ProductionAcceptanceDecision(
                    candidate_id=candidate_id,
                    result=AcceptanceResult.BLOCKED.value,
                    reason="recovery_evidence_missing",
                    evidence_ids=tuple(e.evidence_id for e in evidence),
                )

        if smoke_status != "PASS":
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.PRODUCTION_UNSTABLE.value,
                reason="smoke_fail",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if require_live_evidence and smoke_classification != VerificationClass.LIVE_VERIFIED.value:
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.BLOCKED.value,
                reason="live_smoke_required",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if require_live_evidence and hypercare_classification != VerificationClass.LIVE_VERIFIED.value:
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.BLOCKED.value,
                reason="hypercare_not_live_verified",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if security_p0 > 0:
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.PRODUCTION_UNSTABLE.value,
                reason="security_p0",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if security_p1 > 0:
            return ProductionAcceptanceDecision(
                candidate_id=candidate_id,
                result=AcceptanceResult.PRODUCTION_UNSTABLE.value,
                reason="hypercare_security_p1",
                evidence_ids=tuple(e.evidence_id for e in evidence),
            )
        if hypercare_status != "PASS" or not slo_ok or not finops_ok or not runtime_ok or not recovery_ready or not providers_ok or not side_effects_ok:
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
