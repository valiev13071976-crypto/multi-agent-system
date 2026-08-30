"""Stage-3 handoff gate — fail-closed."""

from __future__ import annotations

from dataclasses import dataclass

from controlled_launch.errors import BLOCKED_BY_STAGE_3, STALE_STAGE3_HANDOFF, ControlledLaunchError
from controlled_launch.models import Stage3Handoff
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.release_gate import ReleaseGateEvaluator


@dataclass
class Stage3HandoffGate:
    """Consumes authoritative Stage-3 evidence; does not recompute Stage 3."""

    config: ValidationConfig | None = None
    evidence_store: EvidenceStore | None = None

    def __post_init__(self):
        if self.config is None:
            self.config = ValidationConfig.from_env()
        if self.evidence_store is None:
            self.evidence_store = EvidenceStore()

    def evaluate(self) -> Stage3Handoff:
        evaluator = ReleaseGateEvaluator(config=self.config, store=self.evidence_store)
        result = evaluator.evaluate()
        latest = self.evidence_store.latest_for_gate("3.17_release_gate") or {}
        metrics = latest.get("safe_metrics") or {}
        stage3_status = "CLOSED" if result.engineering == "PASS" else "OPEN"
        if result.live_validation != "PASS":
            stage3_status = "OPEN"
        handoff = Stage3Handoff(
            evidence_id=result.evidence_id or latest.get("evidence_id", ""),
            stage3_status=stage3_status,
            release_readiness=result.release_readiness,
            p0_count=int(metrics.get("p0_count") or 0),
            p1_count=int(metrics.get("p1_count") or 0),
            release_identity=self.config.release_identity,
            environment=self.config.environment,
            commit_sha=self.config.release_identity,
            deployment_id=str(metrics.get("deployment_id") or ""),
            verified_at=latest.get("completed_at") or latest.get("started_at") or "",
            capacity_envelope=dict(metrics.get("capacity_envelope") or {}),
        )
        return handoff

    def require_ready(self, *, commit_sha: str = "", deployment_id: str = "", environment: str = "") -> Stage3Handoff:
        handoff = self.evaluate()
        if handoff.stage3_status != "CLOSED":
            raise ControlledLaunchError(BLOCKED_BY_STAGE_3, details={"stage3_status": handoff.stage3_status})
        if handoff.release_readiness != "READY":
            raise ControlledLaunchError(BLOCKED_BY_STAGE_3, details={"release_readiness": handoff.release_readiness})
        if handoff.p0_count > 0 or handoff.p1_count > 0:
            raise ControlledLaunchError(BLOCKED_BY_STAGE_3, details={"p0": handoff.p0_count, "p1": handoff.p1_count})
        if commit_sha and handoff.commit_sha and commit_sha != handoff.commit_sha:
            raise ControlledLaunchError(STALE_STAGE3_HANDOFF, details={"expected": handoff.commit_sha, "got": commit_sha})
        if deployment_id and handoff.deployment_id and deployment_id != handoff.deployment_id:
            raise ControlledLaunchError(STALE_STAGE3_HANDOFF, details={"expected": handoff.deployment_id, "got": deployment_id})
        if environment and handoff.environment and environment != handoff.environment:
            raise ControlledLaunchError(STALE_STAGE3_HANDOFF, details={"expected": handoff.environment, "got": environment})
        return handoff

    def allows_live_traffic(self) -> bool:
        try:
            self.require_ready()
            return True
        except ControlledLaunchError:
            return False
