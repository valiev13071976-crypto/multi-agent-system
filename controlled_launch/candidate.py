"""Launch candidate lifecycle."""

from __future__ import annotations

import uuid
from dataclasses import replace

from controlled_launch.errors import (
    CANDIDATE_IMMUTABLE,
    CANDIDATE_NOT_LOCKED,
    ROLLBACK_TARGET_REQUIRED,
    ControlledLaunchError,
)
from controlled_launch.handoff import Stage3HandoffGate
from controlled_launch.models import CandidateStatus, LaunchCandidate


class LaunchCandidateManager:
    def __init__(self, *, handoff_gate: Stage3HandoffGate | None = None):
        self.handoff_gate = handoff_gate or Stage3HandoffGate()

    def draft(
        self,
        *,
        commit_sha: str,
        deployment_id: str,
        environment: str,
        production_url: str,
        rollback_target: str,
        created_by: str,
        schema_version: str = "",
        routing_policy_version: str = "",
        provider_config_versions: dict[str, str] | None = None,
        capacity_envelope: dict | None = None,
        require_stage3: bool = True,
    ) -> LaunchCandidate:
        if not rollback_target.strip():
            raise ControlledLaunchError(ROLLBACK_TARGET_REQUIRED)
        handoff = self.handoff_gate.evaluate()
        if require_stage3:
            try:
                self.handoff_gate.require_ready(
                    commit_sha=commit_sha,
                    deployment_id=deployment_id,
                    environment=environment,
                )
            except ControlledLaunchError:
                pass
        return LaunchCandidate(
            candidate_id=f"lc-{uuid.uuid4().hex[:12]}",
            commit_sha=commit_sha,
            deployment_id=deployment_id,
            environment=environment,
            production_url=production_url,
            rollback_target=rollback_target,
            stage3_evidence_id=handoff.evidence_id,
            schema_version=schema_version,
            routing_policy_version=routing_policy_version,
            provider_config_versions=dict(provider_config_versions or {}),
            capacity_envelope=dict(capacity_envelope or handoff.capacity_envelope),
            created_by=created_by,
        )

    def lock(self, candidate: LaunchCandidate, *, actor: str) -> LaunchCandidate:
        if candidate.status != CandidateStatus.DRAFT.value:
            raise ControlledLaunchError(CANDIDATE_IMMUTABLE, details={"status": candidate.status})
        if not candidate.rollback_target.strip():
            raise ControlledLaunchError(ROLLBACK_TARGET_REQUIRED)
        from datetime import datetime, timezone

        return replace(
            candidate,
            status=CandidateStatus.LOCKED.value,
            locked_at=datetime.now(timezone.utc).isoformat(),
            created_by=actor or candidate.created_by,
        )

    def assert_locked(self, candidate: LaunchCandidate) -> None:
        if candidate.status == CandidateStatus.DRAFT.value:
            raise ControlledLaunchError(CANDIDATE_NOT_LOCKED)

    def assert_immutable_identity(self, original: LaunchCandidate, updated: LaunchCandidate) -> None:
        if original.status != CandidateStatus.DRAFT.value:
            for field in ("candidate_id", "commit_sha", "deployment_id", "rollback_target", "stage3_evidence_id"):
                if getattr(original, field) != getattr(updated, field):
                    raise ControlledLaunchError(CANDIDATE_IMMUTABLE, details={"field": field})

    def with_status(self, candidate: LaunchCandidate, status: CandidateStatus) -> LaunchCandidate:
        self.assert_locked(candidate)
        return replace(candidate, status=status.value)
