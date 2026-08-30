"""Offline routing-policy promotion governance (PATCH-MR-06).

Lifecycle (offline only):

    EVALUATED → CANDIDATE → SHADOW_VALIDATED → CANARY_VALIDATED
        → RELEASE_APPROVED / PRODUCTION_ELIGIBLE

Safety invariants:
- No write path into live ModelRouter, ProviderRegistry, env, or runtime tiebreak.
- PRODUCTION_ELIGIBLE != PRODUCTION_ACTIVE.
- No ``--apply-production`` and no ReleaseGate mutation of production config.
- Production activation is a deliberate external/manual step outside this module.

Shadow and Canary here are **offline evidence contracts**. This module does not
mirror live traffic or deploy a real canary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from evals.models import content_hash, utc_now
from evals.release_gate import GATE_BLOCKED, GATE_FAIL, GATE_PASS, ReleaseGateDecision
from evals.versions import ROUTING_POLICY_VERSION

# --- Governance stages -------------------------------------------------------

STAGE_EVALUATED = "EVALUATED"
STAGE_CANDIDATE = "CANDIDATE"
STAGE_SHADOW_VALIDATED = "SHADOW_VALIDATED"
STAGE_CANARY_VALIDATED = "CANARY_VALIDATED"
STAGE_RELEASE_APPROVED = "RELEASE_APPROVED"
STAGE_PRODUCTION_ELIGIBLE = "PRODUCTION_ELIGIBLE"

# Documented only — never set by this offline governance module.
STAGE_PRODUCTION_ACTIVE = "PRODUCTION_ACTIVE"

GOVERNANCE_STAGES = frozenset(
    {
        STAGE_EVALUATED,
        STAGE_CANDIDATE,
        STAGE_SHADOW_VALIDATED,
        STAGE_CANARY_VALIDATED,
        STAGE_RELEASE_APPROVED,
        STAGE_PRODUCTION_ELIGIBLE,
    }
)

# Legal forward transitions (fail-closed otherwise).
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STAGE_EVALUATED: frozenset({STAGE_CANDIDATE}),
    STAGE_CANDIDATE: frozenset({STAGE_SHADOW_VALIDATED}),
    STAGE_SHADOW_VALIDATED: frozenset({STAGE_CANARY_VALIDATED}),
    STAGE_CANARY_VALIDATED: frozenset({STAGE_RELEASE_APPROVED, STAGE_PRODUCTION_ELIGIBLE}),
    STAGE_RELEASE_APPROVED: frozenset({STAGE_PRODUCTION_ELIGIBLE}),
    STAGE_PRODUCTION_ELIGIBLE: frozenset(),
}

EVIDENCE_PASS = "PASS"
EVIDENCE_FAIL = "FAIL"
EVIDENCE_BLOCKED = "BLOCKED"
EVIDENCE_STATUSES = frozenset({EVIDENCE_PASS, EVIDENCE_FAIL, EVIDENCE_BLOCKED})

METRIC_MEASURED = "measured"
METRIC_UNAVAILABLE = "unavailable"


class PromotionGovernanceError(ValueError):
    """Fail-closed governance rejection (invalid transition / mismatch / evidence)."""

    def __init__(self, reason_code: str, *, details: dict | None = None):
        self.reason_code = str(reason_code)
        self.details = dict(details or {})
        super().__init__(self.reason_code)


def _meta(value) -> Mapping[str, object]:
    from autonomy.models import sanitize_metadata

    return MappingProxyType(sanitize_metadata(value or {}))


@dataclass(frozen=True)
class MetricObservation:
    """Honest metric carrier — never fabricate a PASS from missing data."""

    status: str = METRIC_UNAVAILABLE
    value: float | None = None
    unit: str | None = None

    def __post_init__(self):
        status = str(self.status or METRIC_UNAVAILABLE).strip().lower()
        if status not in {METRIC_MEASURED, METRIC_UNAVAILABLE}:
            raise ValueError(f"invalid_metric_status:{self.status}")
        if status == METRIC_UNAVAILABLE:
            object.__setattr__(self, "value", None)
        object.__setattr__(self, "status", status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "unit": self.unit,
        }


def unavailable_metric() -> MetricObservation:
    return MetricObservation(status=METRIC_UNAVAILABLE)


def measured_metric(value: float, *, unit: str | None = None) -> MetricObservation:
    return MetricObservation(status=METRIC_MEASURED, value=float(value), unit=unit)


@dataclass(frozen=True)
class GovernanceTransition:
    candidate_id: str
    from_stage: str
    to_stage: str
    evidence_ref: str
    result: str
    timestamp: datetime
    base_routing_policy_version: str
    proposed_routing_policy_version: str
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "evidence_ref": self.evidence_ref,
            "result": self.result,
            "timestamp": self.timestamp.isoformat(),
            "base_routing_policy_version": self.base_routing_policy_version,
            "proposed_routing_policy_version": self.proposed_routing_policy_version,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class ShadowEvidence:
    """Offline Shadow validation evidence (not live traffic mirroring)."""

    evidence_id: str
    candidate_id: str
    candidate_version: str
    routing_policy_version: str
    evidence_source: str
    overall_status: str
    quality: MetricObservation = field(default_factory=unavailable_metric)
    latency: MetricObservation = field(default_factory=unavailable_metric)
    cost: MetricObservation = field(default_factory=unavailable_metric)
    routing_divergence: MetricObservation = field(default_factory=unavailable_metric)
    created_at: datetime | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.overall_status not in EVIDENCE_STATUSES:
            raise ValueError(f"invalid_shadow_status:{self.overall_status}")
        stamp = self.created_at or utc_now()
        if stamp.tzinfo is None:
            from datetime import timezone

            stamp = stamp.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "created_at", stamp)
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "candidate_id": self.candidate_id,
            "candidate_version": self.candidate_version,
            "routing_policy_version": self.routing_policy_version,
            "evidence_source": self.evidence_source,
            "overall_status": self.overall_status,
            "quality": self.quality.as_dict(),
            "latency": self.latency.as_dict(),
            "cost": self.cost.as_dict(),
            "routing_divergence": self.routing_divergence.as_dict(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata_safe": dict(self.metadata_safe),
        }


@dataclass(frozen=True)
class CanaryEvidence:
    """Offline Canary validation evidence (not a live canary deployment)."""

    evidence_id: str
    candidate_id: str
    candidate_version: str
    routing_policy_version: str
    evidence_source: str
    overall_status: str
    quality: MetricObservation = field(default_factory=unavailable_metric)
    latency: MetricObservation = field(default_factory=unavailable_metric)
    cost: MetricObservation = field(default_factory=unavailable_metric)
    error_rate: MetricObservation = field(default_factory=unavailable_metric)
    created_at: datetime | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.overall_status not in EVIDENCE_STATUSES:
            raise ValueError(f"invalid_canary_status:{self.overall_status}")
        stamp = self.created_at or utc_now()
        if stamp.tzinfo is None:
            from datetime import timezone

            stamp = stamp.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "created_at", stamp)
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "candidate_id": self.candidate_id,
            "candidate_version": self.candidate_version,
            "routing_policy_version": self.routing_policy_version,
            "evidence_source": self.evidence_source,
            "overall_status": self.overall_status,
            "quality": self.quality.as_dict(),
            "latency": self.latency.as_dict(),
            "cost": self.cost.as_dict(),
            "error_rate": self.error_rate.as_dict(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata_safe": dict(self.metadata_safe),
        }


@dataclass(frozen=True)
class CandidatePolicy:
    """Versioned immutable candidate routing-policy promotion artifact.

    ``production_eligible`` may become True after full governance PASS.
    ``production_active`` is always False in this offline module — activation is
    an external manual step.
    """

    candidate_id: str
    candidate_version: str
    base_routing_policy_version: str
    proposed_routing_policy_version: str
    stage: str
    eval_suite_id: str
    eval_suite_version: str
    eval_run_id: str
    eval_manifest_hash: str
    model_profile_version: str
    provider_profile_versions: Mapping[str, object] = field(default_factory=dict)
    eval_artifact_ref: str = ""
    shadow_evidence_id: str | None = None
    canary_evidence_id: str | None = None
    release_gate_decision: str | None = None
    release_gate_reason_codes: tuple[str, ...] = ()
    production_eligible: bool = False
    production_active: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    transitions: tuple[GovernanceTransition, ...] = ()
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.stage not in GOVERNANCE_STAGES:
            raise ValueError(f"invalid_governance_stage:{self.stage}")
        if self.production_active:
            raise PromotionGovernanceError(
                "production_active_forbidden_in_offline_governance",
                details={"candidate_id": self.candidate_id},
            )
        stamp = self.created_at or utc_now()
        if stamp.tzinfo is None:
            from datetime import timezone

            stamp = stamp.replace(tzinfo=timezone.utc)
        updated = self.updated_at or stamp
        if updated.tzinfo is None:
            from datetime import timezone

            updated = updated.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "created_at", stamp)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "provider_profile_versions", _meta(self.provider_profile_versions))
        object.__setattr__(self, "release_gate_reason_codes", tuple(self.release_gate_reason_codes))
        object.__setattr__(self, "transitions", tuple(self.transitions))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
        object.__setattr__(self, "production_active", False)

    @property
    def content_hash(self) -> str:
        return content_hash(self.identity_payload())

    def identity_payload(self) -> dict[str, Any]:
        """Stable identity fields for reproducibility (no secrets/prompts)."""

        return {
            "candidate_id": self.candidate_id,
            "candidate_version": self.candidate_version,
            "base_routing_policy_version": self.base_routing_policy_version,
            "proposed_routing_policy_version": self.proposed_routing_policy_version,
            "eval_suite_id": self.eval_suite_id,
            "eval_suite_version": self.eval_suite_version,
            "eval_run_id": self.eval_run_id,
            "eval_manifest_hash": self.eval_manifest_hash,
            "eval_artifact_ref": self.eval_artifact_ref,
            "model_profile_version": self.model_profile_version,
            "provider_profile_versions": dict(self.provider_profile_versions),
            "shadow_evidence_id": self.shadow_evidence_id,
            "canary_evidence_id": self.canary_evidence_id,
            "release_gate_decision": self.release_gate_decision,
            "stage": self.stage,
            "production_eligible": self.production_eligible,
            "production_active": False,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.identity_payload()
        payload.update(
            {
                "content_hash": self.content_hash,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "release_gate_reason_codes": list(self.release_gate_reason_codes),
                "transitions": [t.as_dict() for t in self.transitions],
                "metadata_safe": dict(self.metadata_safe),
            }
        )
        return payload


def _require_transition(from_stage: str, to_stage: str) -> None:
    allowed = _LEGAL_TRANSITIONS.get(from_stage, frozenset())
    if to_stage not in allowed:
        raise PromotionGovernanceError(
            "invalid_governance_transition",
            details={"from_stage": from_stage, "to_stage": to_stage},
        )


def _require_policy_match(
    *,
    candidate: CandidatePolicy,
    evidence_policy_version: str,
    context: str,
) -> None:
    expected = str(candidate.proposed_routing_policy_version or "")
    got = str(evidence_policy_version or "")
    if not expected or expected != got:
        raise PromotionGovernanceError(
            "routing_policy_version_mismatch",
            details={
                "context": context,
                "candidate_id": candidate.candidate_id,
                "proposed_routing_policy_version": expected,
                "evidence_routing_policy_version": got,
            },
        )


class PromotionGovernor:
    """Offline promotion lifecycle — never mutates live routing configuration."""

    def create_candidate(
        self,
        *,
        candidate_id: str,
        candidate_version: str,
        base_routing_policy_version: str,
        proposed_routing_policy_version: str,
        eval_suite_id: str,
        eval_suite_version: str,
        eval_run_id: str,
        eval_manifest_hash: str,
        model_profile_version: str,
        provider_profile_versions: Mapping[str, object] | None = None,
        eval_artifact_ref: str = "",
        metadata_safe: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> CandidatePolicy:
        """Create a CANDIDATE from eval evidence. Does not mutate live routing."""

        base = str(base_routing_policy_version or "").strip()
        proposed = str(proposed_routing_policy_version or "").strip()
        if not base or not proposed:
            raise PromotionGovernanceError(
                "routing_policy_version_required",
                details={"base": base, "proposed": proposed},
            )
        if not str(candidate_id or "").strip() or not str(candidate_version or "").strip():
            raise PromotionGovernanceError("candidate_identity_required")
        if not str(eval_run_id or "").strip():
            raise PromotionGovernanceError("eval_run_required")

        stamp = now or utc_now()
        transition = GovernanceTransition(
            candidate_id=str(candidate_id),
            from_stage=STAGE_EVALUATED,
            to_stage=STAGE_CANDIDATE,
            evidence_ref=str(eval_artifact_ref or eval_run_id),
            result=EVIDENCE_PASS,
            timestamp=stamp,
            base_routing_policy_version=base,
            proposed_routing_policy_version=proposed,
            reason_codes=("candidate_created_from_eval",),
        )
        return CandidatePolicy(
            candidate_id=str(candidate_id),
            candidate_version=str(candidate_version),
            base_routing_policy_version=base,
            proposed_routing_policy_version=proposed,
            stage=STAGE_CANDIDATE,
            eval_suite_id=str(eval_suite_id),
            eval_suite_version=str(eval_suite_version),
            eval_run_id=str(eval_run_id),
            eval_manifest_hash=str(eval_manifest_hash or ""),
            model_profile_version=str(model_profile_version),
            provider_profile_versions=dict(provider_profile_versions or {}),
            eval_artifact_ref=str(eval_artifact_ref or eval_run_id),
            production_eligible=False,
            production_active=False,
            created_at=stamp,
            updated_at=stamp,
            transitions=(transition,),
            metadata_safe=dict(metadata_safe or {}),
        )

    def apply_shadow(
        self,
        candidate: CandidatePolicy,
        evidence: ShadowEvidence,
        *,
        now: datetime | None = None,
    ) -> CandidatePolicy:
        if candidate.stage != STAGE_CANDIDATE:
            raise PromotionGovernanceError(
                "shadow_requires_candidate_stage",
                details={"stage": candidate.stage},
            )
        if evidence.candidate_id != candidate.candidate_id:
            raise PromotionGovernanceError(
                "shadow_candidate_id_mismatch",
                details={
                    "candidate_id": candidate.candidate_id,
                    "evidence_candidate_id": evidence.candidate_id,
                },
            )
        _require_policy_match(
            candidate=candidate,
            evidence_policy_version=evidence.routing_policy_version,
            context="shadow",
        )
        if evidence.overall_status != EVIDENCE_PASS:
            raise PromotionGovernanceError(
                "shadow_evidence_not_pass",
                details={
                    "overall_status": evidence.overall_status,
                    "candidate_id": candidate.candidate_id,
                },
            )
        _require_transition(candidate.stage, STAGE_SHADOW_VALIDATED)
        stamp = now or utc_now()
        transition = GovernanceTransition(
            candidate_id=candidate.candidate_id,
            from_stage=candidate.stage,
            to_stage=STAGE_SHADOW_VALIDATED,
            evidence_ref=evidence.evidence_id,
            result=EVIDENCE_PASS,
            timestamp=stamp,
            base_routing_policy_version=candidate.base_routing_policy_version,
            proposed_routing_policy_version=candidate.proposed_routing_policy_version,
            reason_codes=("shadow_validated",),
        )
        return CandidatePolicy(
            candidate_id=candidate.candidate_id,
            candidate_version=candidate.candidate_version,
            base_routing_policy_version=candidate.base_routing_policy_version,
            proposed_routing_policy_version=candidate.proposed_routing_policy_version,
            stage=STAGE_SHADOW_VALIDATED,
            eval_suite_id=candidate.eval_suite_id,
            eval_suite_version=candidate.eval_suite_version,
            eval_run_id=candidate.eval_run_id,
            eval_manifest_hash=candidate.eval_manifest_hash,
            model_profile_version=candidate.model_profile_version,
            provider_profile_versions=dict(candidate.provider_profile_versions),
            eval_artifact_ref=candidate.eval_artifact_ref,
            shadow_evidence_id=evidence.evidence_id,
            canary_evidence_id=candidate.canary_evidence_id,
            release_gate_decision=candidate.release_gate_decision,
            release_gate_reason_codes=candidate.release_gate_reason_codes,
            production_eligible=False,
            production_active=False,
            created_at=candidate.created_at,
            updated_at=stamp,
            transitions=candidate.transitions + (transition,),
            metadata_safe=dict(candidate.metadata_safe),
        )

    def apply_canary(
        self,
        candidate: CandidatePolicy,
        evidence: CanaryEvidence,
        *,
        now: datetime | None = None,
    ) -> CandidatePolicy:
        if candidate.stage != STAGE_SHADOW_VALIDATED:
            raise PromotionGovernanceError(
                "canary_requires_shadow_validated",
                details={"stage": candidate.stage},
            )
        if evidence.candidate_id != candidate.candidate_id:
            raise PromotionGovernanceError(
                "canary_candidate_id_mismatch",
                details={
                    "candidate_id": candidate.candidate_id,
                    "evidence_candidate_id": evidence.candidate_id,
                },
            )
        _require_policy_match(
            candidate=candidate,
            evidence_policy_version=evidence.routing_policy_version,
            context="canary",
        )
        if evidence.overall_status != EVIDENCE_PASS:
            raise PromotionGovernanceError(
                "canary_evidence_not_pass",
                details={
                    "overall_status": evidence.overall_status,
                    "candidate_id": candidate.candidate_id,
                },
            )
        _require_transition(candidate.stage, STAGE_CANARY_VALIDATED)
        stamp = now or utc_now()
        transition = GovernanceTransition(
            candidate_id=candidate.candidate_id,
            from_stage=candidate.stage,
            to_stage=STAGE_CANARY_VALIDATED,
            evidence_ref=evidence.evidence_id,
            result=EVIDENCE_PASS,
            timestamp=stamp,
            base_routing_policy_version=candidate.base_routing_policy_version,
            proposed_routing_policy_version=candidate.proposed_routing_policy_version,
            reason_codes=("canary_validated",),
        )
        return CandidatePolicy(
            candidate_id=candidate.candidate_id,
            candidate_version=candidate.candidate_version,
            base_routing_policy_version=candidate.base_routing_policy_version,
            proposed_routing_policy_version=candidate.proposed_routing_policy_version,
            stage=STAGE_CANARY_VALIDATED,
            eval_suite_id=candidate.eval_suite_id,
            eval_suite_version=candidate.eval_suite_version,
            eval_run_id=candidate.eval_run_id,
            eval_manifest_hash=candidate.eval_manifest_hash,
            model_profile_version=candidate.model_profile_version,
            provider_profile_versions=dict(candidate.provider_profile_versions),
            eval_artifact_ref=candidate.eval_artifact_ref,
            shadow_evidence_id=candidate.shadow_evidence_id,
            canary_evidence_id=evidence.evidence_id,
            release_gate_decision=candidate.release_gate_decision,
            release_gate_reason_codes=candidate.release_gate_reason_codes,
            production_eligible=False,
            production_active=False,
            created_at=candidate.created_at,
            updated_at=stamp,
            transitions=candidate.transitions + (transition,),
            metadata_safe=dict(candidate.metadata_safe),
        )

    def apply_release_gate(
        self,
        candidate: CandidatePolicy,
        gate: ReleaseGateDecision,
        *,
        expected_routing_policy_version: str | None = None,
        now: datetime | None = None,
    ) -> CandidatePolicy:
        """Integrate offline ReleaseGate into the promotion lifecycle.

        Requires CANARY_VALIDATED. PASS → PRODUCTION_ELIGIBLE (still not active).
        FAIL/BLOCKED or missing stages → fail closed (not eligible).
        """

        if candidate.stage != STAGE_CANARY_VALIDATED:
            raise PromotionGovernanceError(
                "release_gate_requires_canary_validated",
                details={
                    "stage": candidate.stage,
                    "gate_decision": gate.decision,
                },
            )
        policy_check = str(
            expected_routing_policy_version
            if expected_routing_policy_version is not None
            else candidate.proposed_routing_policy_version
        )
        _require_policy_match(
            candidate=candidate,
            evidence_policy_version=policy_check,
            context="release_gate",
        )
        if gate.decision != GATE_PASS:
            raise PromotionGovernanceError(
                "release_gate_not_pass",
                details={
                    "gate_decision": gate.decision,
                    "reason_codes": list(gate.reason_codes),
                    "candidate_id": candidate.candidate_id,
                },
            )
        if not candidate.shadow_evidence_id or not candidate.canary_evidence_id:
            raise PromotionGovernanceError(
                "release_gate_missing_required_stages",
                details={
                    "shadow_evidence_id": candidate.shadow_evidence_id,
                    "canary_evidence_id": candidate.canary_evidence_id,
                },
            )

        stamp = now or utc_now()
        t_approved = GovernanceTransition(
            candidate_id=candidate.candidate_id,
            from_stage=candidate.stage,
            to_stage=STAGE_RELEASE_APPROVED,
            evidence_ref=f"release_gate:{gate.decision}",
            result=GATE_PASS,
            timestamp=stamp,
            base_routing_policy_version=candidate.base_routing_policy_version,
            proposed_routing_policy_version=candidate.proposed_routing_policy_version,
            reason_codes=("release_gate_pass",) + tuple(gate.reason_codes),
        )
        t_eligible = GovernanceTransition(
            candidate_id=candidate.candidate_id,
            from_stage=STAGE_RELEASE_APPROVED,
            to_stage=STAGE_PRODUCTION_ELIGIBLE,
            evidence_ref=f"release_gate:{gate.decision}",
            result=GATE_PASS,
            timestamp=stamp,
            base_routing_policy_version=candidate.base_routing_policy_version,
            proposed_routing_policy_version=candidate.proposed_routing_policy_version,
            reason_codes=("production_eligible",),
        )
        return CandidatePolicy(
            candidate_id=candidate.candidate_id,
            candidate_version=candidate.candidate_version,
            base_routing_policy_version=candidate.base_routing_policy_version,
            proposed_routing_policy_version=candidate.proposed_routing_policy_version,
            stage=STAGE_PRODUCTION_ELIGIBLE,
            eval_suite_id=candidate.eval_suite_id,
            eval_suite_version=candidate.eval_suite_version,
            eval_run_id=candidate.eval_run_id,
            eval_manifest_hash=candidate.eval_manifest_hash,
            model_profile_version=candidate.model_profile_version,
            provider_profile_versions=dict(candidate.provider_profile_versions),
            eval_artifact_ref=candidate.eval_artifact_ref,
            shadow_evidence_id=candidate.shadow_evidence_id,
            canary_evidence_id=candidate.canary_evidence_id,
            release_gate_decision=GATE_PASS,
            release_gate_reason_codes=tuple(gate.reason_codes),
            production_eligible=True,
            production_active=False,
            created_at=candidate.created_at,
            updated_at=stamp,
            transitions=candidate.transitions + (t_approved, t_eligible),
            metadata_safe=dict(candidate.metadata_safe),
        )


def default_base_routing_policy_version() -> str:
    """Current production routing policy version pin (code constant)."""

    return str(ROUTING_POLICY_VERSION)


# Explicit: this module must never export an apply-production helper.
assert not hasattr(PromotionGovernor, "apply_production")
assert not hasattr(PromotionGovernor, "activate_production")
