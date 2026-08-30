"""Final production candidate lock."""

from __future__ import annotations

import hashlib

from production_activation.errors import CANDIDATE_IMMUTABLE, CANDIDATE_INVALIDATED, ProductionActivationError
from production_activation.handoff import Stage5HandoffGate
from production_activation.models import FinalProductionCandidate, Stage5Handoff


def candidate_fingerprint(candidate: FinalProductionCandidate) -> str:
    raw = "|".join(
        [
            candidate.candidate_id,
            candidate.commit_sha,
            candidate.deployment_id,
            candidate.environment,
            candidate.rollback_target,
            candidate.routing_policy_version,
            candidate.traffic_policy_version,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class FinalCandidateLock:
    def __init__(self, *, handoff_gate: Stage5HandoffGate | None = None):
        self.handoff_gate = handoff_gate or Stage5HandoffGate()
        self._locked: FinalProductionCandidate | None = None

    @property
    def locked(self) -> FinalProductionCandidate | None:
        return self._locked

    def lock_from_handoff(self, handoff: Stage5Handoff, *, production_url: str, routing_policy_version: str = "", traffic_policy_version: str = "") -> FinalProductionCandidate:
        self.handoff_gate.require_ready(
            candidate_id=handoff.candidate_id,
            commit_sha=handoff.commit_sha,
            deployment_id=handoff.deployment_id,
            environment=handoff.environment,
        )
        candidate = FinalProductionCandidate(
            candidate_id=handoff.candidate_id,
            commit_sha=handoff.commit_sha,
            deployment_id=handoff.deployment_id,
            environment=handoff.environment,
            production_url=production_url,
            rollback_target=handoff.rollback_target,
            stage3_evidence_id=handoff.stage3_evidence_id,
            stage4_evidence_id=handoff.stage4_evidence_id,
            routing_policy_version=routing_policy_version,
            traffic_policy_version=traffic_policy_version,
            monitoring_state="ready" if handoff.monitoring_ready else "unknown",
            alert_state="ready" if handoff.alerts_ready else "unknown",
            backup_state="ready" if handoff.backup_ready else "unknown",
        )
        fp = candidate_fingerprint(candidate)
        object.__setattr__(candidate, "fingerprint", fp)
        self._locked = candidate
        return candidate

    def assert_immutable(self, updated: FinalProductionCandidate) -> None:
        if self._locked is None:
            return
        for field in ("candidate_id", "commit_sha", "deployment_id", "rollback_target", "stage3_evidence_id", "stage4_evidence_id"):
            if getattr(self._locked, field) != getattr(updated, field):
                raise ProductionActivationError(CANDIDATE_IMMUTABLE, details={"field": field})

    def invalidate_on_material_change(self, *, commit_sha: str, deployment_id: str) -> None:
        if self._locked is None:
            return
        if commit_sha != self._locked.commit_sha or deployment_id != self._locked.deployment_id:
            self._locked = None
            raise ProductionActivationError(CANDIDATE_INVALIDATED, details={"commit_sha": commit_sha, "deployment_id": deployment_id})
