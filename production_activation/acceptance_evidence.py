"""Durable evidence selection and derivation for final live acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from production_activation.models import FinalProductionCandidate, ProductionActivationEvidence, VerificationClass


def _is_live_smoke(evidence: ProductionActivationEvidence) -> bool:
    metrics = evidence.safe_metrics or {}
    if metrics.get("evidence_kind") == "post_launch_smoke":
        return True
    return "checks" in metrics and metrics.get("mode") == "live"


def _is_hypercare(evidence: ProductionActivationEvidence) -> bool:
    metrics = evidence.safe_metrics or {}
    if metrics.get("evidence_kind") == "hypercare":
        return True
    return "duration_seconds" in metrics
from production_activation.stage5_gate import MANDATORY_STAGE5_GATES

RECOVERY_GATES = ("5.11_backup_recovery", "5.12_rollback_readiness")


@dataclass(frozen=True)
class BoundLiveEvidence:
    smoke: ProductionActivationEvidence | None
    hypercare: ProductionActivationEvidence | None
    smoke_status: str
    smoke_classification: str
    hypercare_status: str
    hypercare_classification: str


@dataclass(frozen=True)
class LiveAcceptanceInputs:
    bound_smoke: bool
    bound_hypercare: bool
    smoke_status: str
    smoke_classification: str
    hypercare_status: str
    hypercare_classification: str
    security_p0: int
    security_p1: int
    recovery_ready: bool
    block_reason: str = ""


def _release_matches(metrics: dict[str, Any], release_identity: str) -> bool:
    if not release_identity:
        return True
    return str(metrics.get("release_identity") or "") == release_identity


def _plan_matches(evidence: ProductionActivationEvidence, plan_id: str) -> bool:
    if not plan_id:
        return True
    metrics = evidence.safe_metrics or {}
    return str(evidence.plan_id or metrics.get("plan_id") or "") == plan_id


def _attempt_matches(evidence: ProductionActivationEvidence, attempt_id: str) -> bool:
    if not attempt_id:
        return True
    ev_attempt = str(evidence.attempt_id or (evidence.safe_metrics or {}).get("attempt_id") or "")
    if not ev_attempt:
        return True
    return ev_attempt == attempt_id


def _smoke_check_pass(smoke: ProductionActivationEvidence, name: str) -> bool:
    for check in (smoke.safe_metrics or {}).get("checks") or []:
        if check.get("name") == name and check.get("status") == "PASS":
            return True
    return False


def _gate_pass(
    evidence: list[ProductionActivationEvidence],
    *,
    gate: str,
    release_identity: str,
) -> bool:
    for item in evidence:
        metrics = item.safe_metrics or {}
        if metrics.get("gate") != gate:
            continue
        if metrics.get("status") != "PASS":
            return False
        if release_identity and not _release_matches(metrics, release_identity):
            continue
        return True
    return False


def select_bound_live_smoke(
    evidence: list[ProductionActivationEvidence],
    *,
    candidate_id: str,
    plan_id: str,
    release_identity: str,
    attempt_id: str = "",
) -> ProductionActivationEvidence | None:
    for item in reversed(evidence):
        if item.candidate_id != candidate_id:
            continue
        metrics = item.safe_metrics or {}
        if not _is_live_smoke(item):
            continue
        if item.classification != VerificationClass.LIVE_VERIFIED.value:
            continue
        if metrics.get("status") != "PASS":
            continue
        if not _plan_matches(item, plan_id):
            continue
        if not _release_matches(metrics, release_identity):
            continue
        if not _attempt_matches(item, attempt_id):
            continue
        return item
    return None


def select_bound_live_hypercare(
    evidence: list[ProductionActivationEvidence],
    *,
    candidate_id: str,
    plan_id: str,
    release_identity: str,
) -> ProductionActivationEvidence | None:
    for item in reversed(evidence):
        if item.candidate_id != candidate_id:
            continue
        metrics = item.safe_metrics or {}
        if not _is_hypercare(item):
            continue
        if item.classification != VerificationClass.LIVE_VERIFIED.value:
            continue
        if metrics.get("status") != "PASS":
            continue
        if not _plan_matches(item, plan_id):
            continue
        if not _release_matches(metrics, release_identity):
            continue
        return item
    return None


def _hypercare_min_requests(hypercare: ProductionActivationEvidence, hypercare_session: dict[str, Any] | None) -> int:
    session_policy = (hypercare_session or {}).get("policy") or {}
    metrics_policy = (hypercare.safe_metrics or {}).get("policy") or {}
    for source in (session_policy, metrics_policy):
        if "min_requests" in source:
            return int(source.get("min_requests") or 0)
    return 10


def _hypercare_metrics_complete(
    hypercare: ProductionActivationEvidence,
    hypercare_session: dict[str, Any] | None,
) -> tuple[bool, int, int, int]:
    metrics = dict(hypercare.safe_metrics or {})
    session = hypercare_session or {}
    requests = metrics.get("requests")
    p0_count = metrics.get("p0_count")
    p1_count = metrics.get("p1_count")
    if requests is None and "requests" in session:
        requests = session.get("requests")
    if p0_count is None and "p0_count" in session:
        p0_count = session.get("p0_count")
    if p1_count is None and "p1_count" in session:
        p1_count = session.get("p1_count")
    metrics_provided = bool(metrics.get("metrics_provided") or session.get("metrics_provided"))
    if requests is None or p0_count is None or p1_count is None:
        return False, 0, 0, 0
    if not metrics_provided and requests == 0:
        return False, 0, 0, 0
    return True, int(requests), int(p0_count), int(p1_count)


def derive_security_from_hypercare(
    hypercare: ProductionActivationEvidence | None,
    *,
    hypercare_session: dict[str, Any] | None,
) -> tuple[int, int, str]:
    if hypercare is None:
        return 0, 0, "live_hypercare_missing_or_unbound"
    if hypercare.classification != VerificationClass.LIVE_VERIFIED.value:
        return 0, 0, "hypercare_not_live_verified"
    metrics = hypercare.safe_metrics or {}
    if metrics.get("status") != "PASS":
        return 0, 0, "hypercare_not_pass"
    complete, requests, p0_count, p1_count = _hypercare_metrics_complete(hypercare, hypercare_session)
    if not complete:
        return 0, 0, "hypercare_metrics_missing"
    min_requests = _hypercare_min_requests(hypercare, hypercare_session)
    if requests < min_requests:
        return 0, 0, "hypercare_insufficient_requests"
    if p0_count > 0:
        return p0_count, p1_count, "hypercare_security_p0"
    if p1_count > 0:
        return p0_count, p1_count, "hypercare_security_p1"
    return p0_count, p1_count, ""


def evaluate_providers_from_smoke(
    smoke: ProductionActivationEvidence | None,
    required_providers: tuple[str, ...] | list[str],
) -> bool:
    if not required_providers:
        return True
    if smoke is None:
        return False
    for provider_id in required_providers:
        if provider_id == "openai" and not _smoke_check_pass(smoke, "ai"):
            return False
    return True


def evaluate_durable_recovery_ready(
    evidence: list[ProductionActivationEvidence],
    *,
    candidate: FinalProductionCandidate | None,
    smoke: ProductionActivationEvidence | None,
    release_identity: str,
) -> tuple[bool, str]:
    for gate in RECOVERY_GATES:
        if gate not in MANDATORY_STAGE5_GATES:
            continue
        if not _gate_pass(evidence, gate=gate, release_identity=release_identity):
            return False, "recovery_evidence_missing"
    if smoke is None or not _smoke_check_pass(smoke, "persistence"):
        return False, "recovery_persistence_missing"
    if candidate is not None and str(candidate.backup_state or "") not in {"ready", "verified"}:
        return False, "recovery_candidate_backup_not_ready"
    return True, ""


def build_live_acceptance_inputs(
    evidence: list[ProductionActivationEvidence],
    *,
    candidate_id: str,
    plan_id: str,
    release_identity: str,
    attempt_id: str = "",
    candidate: FinalProductionCandidate | None = None,
    hypercare_session: dict[str, Any] | None = None,
) -> LiveAcceptanceInputs:
    smoke = select_bound_live_smoke(
        evidence,
        candidate_id=candidate_id,
        plan_id=plan_id,
        release_identity=release_identity,
        attempt_id=attempt_id,
    )
    hypercare = select_bound_live_hypercare(
        evidence,
        candidate_id=candidate_id,
        plan_id=plan_id,
        release_identity=release_identity,
    )
    bound_smoke = smoke is not None
    bound_hypercare = hypercare is not None
    smoke_status = (smoke.safe_metrics or {}).get("status", "FAIL") if smoke else "FAIL"
    smoke_classification = smoke.classification if smoke else ""
    hypercare_status = (hypercare.safe_metrics or {}).get("status", "FAIL") if hypercare else "FAIL"
    hypercare_classification = hypercare.classification if hypercare else ""

    if not bound_smoke:
        return LiveAcceptanceInputs(
            bound_smoke=False,
            bound_hypercare=bound_hypercare,
            smoke_status=smoke_status,
            smoke_classification=smoke_classification,
            hypercare_status=hypercare_status,
            hypercare_classification=hypercare_classification,
            security_p0=0,
            security_p1=0,
            recovery_ready=False,
            block_reason="live_smoke_missing_or_unbound",
        )
    if not bound_hypercare:
        return LiveAcceptanceInputs(
            bound_smoke=True,
            bound_hypercare=False,
            smoke_status=smoke_status,
            smoke_classification=smoke_classification,
            hypercare_status=hypercare_status,
            hypercare_classification=hypercare_classification,
            security_p0=0,
            security_p1=0,
            recovery_ready=False,
            block_reason="live_hypercare_missing_or_unbound",
        )

    security_p0, security_p1, security_reason = derive_security_from_hypercare(
        hypercare,
        hypercare_session=hypercare_session,
    )
    if security_reason:
        return LiveAcceptanceInputs(
            bound_smoke=True,
            bound_hypercare=True,
            smoke_status=smoke_status,
            smoke_classification=smoke_classification,
            hypercare_status=hypercare_status,
            hypercare_classification=hypercare_classification,
            security_p0=security_p0,
            security_p1=security_p1,
            recovery_ready=False,
            block_reason=security_reason,
        )

    recovery_ready, recovery_reason = evaluate_durable_recovery_ready(
        evidence,
        candidate=candidate,
        smoke=smoke,
        release_identity=release_identity,
    )
    if not recovery_ready:
        return LiveAcceptanceInputs(
            bound_smoke=True,
            bound_hypercare=True,
            smoke_status=smoke_status,
            smoke_classification=smoke_classification,
            hypercare_status=hypercare_status,
            hypercare_classification=hypercare_classification,
            security_p0=security_p0,
            security_p1=security_p1,
            recovery_ready=False,
            block_reason=recovery_reason,
        )

    return LiveAcceptanceInputs(
        bound_smoke=True,
        bound_hypercare=True,
        smoke_status=smoke_status,
        smoke_classification=smoke_classification,
        hypercare_status=hypercare_status,
        hypercare_classification=hypercare_classification,
        security_p0=security_p0,
        security_p1=security_p1,
        recovery_ready=True,
        block_reason="",
    )
