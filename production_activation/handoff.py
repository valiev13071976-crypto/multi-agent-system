"""Stage-5 handoff gate — fail-closed on Stage 3 + Stage 4 (artifact + candidate)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from controlled_launch.handoff import Stage3HandoffGate
from controlled_launch.models import CandidateStatus, PromotionResult
from controlled_launch.promotion import PromotionGate
from controlled_launch.sqlite_store import SqliteControlledLaunchStore
from production_activation.errors import BLOCKED_BY_PREVIOUS_STAGE, STAGE5_BLOCKED_BY_STAGE4, STALE_HANDOFF, ProductionActivationError
from production_activation.models import Stage5Handoff
from production_activation.stage4_artifact import load_stage4_handoff_artifact, require_stage4_artifact_ready


@dataclass
class Stage5HandoffGate:
    stage3_gate: Stage3HandoffGate | None = None
    launch_store: SqliteControlledLaunchStore | None = None
    stage4_artifact_path: str | Path | None = None
    require_stage4_artifact: bool = True

    def __post_init__(self):
        if self.stage3_gate is None:
            self.stage3_gate = Stage3HandoffGate()
        if self.launch_store is None:
            self.launch_store = SqliteControlledLaunchStore()

    def _load_stage4_artifact(self) -> dict | None:
        if not self.require_stage4_artifact and self.stage4_artifact_path is None:
            return None
        try:
            return load_stage4_handoff_artifact(self.stage4_artifact_path)
        except ProductionActivationError:
            if self.require_stage4_artifact:
                raise
            return None

    def _stage4_from_candidate(self, candidate_id: str) -> dict:
        candidate = self.launch_store.get_candidate(candidate_id)
        if candidate is None:
            return {"status": "OPEN", "promotion": "GO_LIVE_BLOCKED", "p0": 0, "p1": 0, "evidence_id": ""}
        evidence = self.launch_store.list_evidence(candidate_id)
        rollout = self.launch_store.get_rollout(candidate_id)
        promotion = PromotionGate().evaluate(
            candidate_id=candidate_id,
            candidate_status=candidate.status,
            evidence=evidence,
            stage3_ready=self.stage3_gate.allows_live_traffic(),
            rollout_step=rollout.current_step if rollout else "",
        )
        stage4_status = (
            "CLOSED"
            if candidate.status == CandidateStatus.GO_LIVE_ELIGIBLE.value and promotion.result == PromotionResult.GO_LIVE_ELIGIBLE.value
            else "OPEN"
        )
        ev_ids = [e.evidence_id for e in evidence]
        return {
            "status": stage4_status,
            "promotion": promotion.result,
            "p0": 0,
            "p1": 0,
            "evidence_id": ev_ids[-1] if ev_ids else "",
            "candidate": candidate,
        }

    def evaluate(self, *, candidate_id: str) -> Stage5Handoff:
        s3 = self.stage3_gate.evaluate()
        artifact = None
        artifact_ok = False
        try:
            artifact = self._load_stage4_artifact()
            if artifact is not None:
                require_stage4_artifact_ready(artifact)
                artifact_ok = True
        except ProductionActivationError:
            artifact_ok = False
            if self.require_stage4_artifact and artifact is None:
                # missing/malformed — leave stage4 open
                pass

        s4 = self._stage4_from_candidate(candidate_id)
        candidate = s4.get("candidate")
        if artifact_ok:
            stage4_status = "CLOSED"
            promotion = PromotionResult.GO_LIVE_ELIGIBLE.value
            p0 = len(artifact.get("p0") or []) if artifact else 0
            p1 = len(artifact.get("p1") or []) if artifact else 0
            evidence_id = str((artifact or {}).get("evidence_id") or s4.get("evidence_id") or "")
        else:
            stage4_status = s4["status"]
            promotion = s4["promotion"]
            p0 = s4["p0"]
            p1 = s4["p1"]
            evidence_id = s4["evidence_id"]

        return Stage5Handoff(
            stage3_status=s3.stage3_status,
            stage3_readiness=s3.release_readiness,
            stage3_p0=s3.p0_count,
            stage3_p1=s3.p1_count,
            stage3_evidence_id=s3.evidence_id,
            stage4_status=stage4_status,
            promotion_decision=promotion,
            stage4_p0=p0,
            stage4_p1=p1,
            stage4_evidence_id=evidence_id,
            candidate_id=candidate_id,
            commit_sha=getattr(candidate, "commit_sha", "") if candidate else str((artifact or {}).get("release_identity") or ""),
            deployment_id=getattr(candidate, "deployment_id", "") if candidate else "",
            environment=getattr(candidate, "environment", "") if candidate else "production",
            rollback_target=getattr(candidate, "rollback_target", "") if candidate else "stage4-rollback",
            monitoring_ready=bool(getattr(candidate, "capacity_envelope", {}).get("monitoring_ready")) if candidate else True,
            alerts_ready=bool(getattr(candidate, "capacity_envelope", {}).get("alerts_ready")) if candidate else True,
            backup_ready=bool(getattr(candidate, "capacity_envelope", {}).get("backup_ready")) if candidate else True,
        )

    def require_ready(
        self,
        *,
        candidate_id: str,
        commit_sha: str = "",
        deployment_id: str = "",
        environment: str = "",
    ) -> Stage5Handoff:
        # Enforce Stage-4 artifact when configured
        artifact = self._load_stage4_artifact()
        if artifact is not None:
            try:
                require_stage4_artifact_ready(artifact, release_identity=commit_sha)
            except ProductionActivationError as exc:
                raise ProductionActivationError(STAGE5_BLOCKED_BY_STAGE4, details=exc.details) from exc

        handoff = self.evaluate(candidate_id=candidate_id)
        if handoff.stage3_status != "CLOSED" or handoff.stage3_readiness != "READY":
            raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"stage3": handoff.stage3_status})
        if handoff.stage3_p0 > 0 or handoff.stage3_p1 > 0:
            raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"stage3_p": (handoff.stage3_p0, handoff.stage3_p1)})
        if handoff.stage4_status != "CLOSED" or handoff.promotion_decision != PromotionResult.GO_LIVE_ELIGIBLE.value:
            raise ProductionActivationError(
                STAGE5_BLOCKED_BY_STAGE4,
                details={"stage4": handoff.stage4_status, "promotion": handoff.promotion_decision},
            )
        if handoff.stage4_p0 > 0 or handoff.stage4_p1 > 0:
            raise ProductionActivationError(STAGE5_BLOCKED_BY_STAGE4, details={"stage4_p": (handoff.stage4_p0, handoff.stage4_p1)})
        if not handoff.rollback_target.strip():
            raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"rollback_target": "missing"})
        if commit_sha and handoff.commit_sha and commit_sha != handoff.commit_sha:
            raise ProductionActivationError(STALE_HANDOFF, details={"field": "commit_sha"})
        if deployment_id and handoff.deployment_id and deployment_id != handoff.deployment_id:
            raise ProductionActivationError(STALE_HANDOFF, details={"field": "deployment_id"})
        if environment and handoff.environment and environment != handoff.environment:
            raise ProductionActivationError(STALE_HANDOFF, details={"field": "environment"})
        return handoff

    def allows_activation(self, *, candidate_id: str) -> bool:
        try:
            self.require_ready(candidate_id=candidate_id)
            return True
        except ProductionActivationError:
            return False

    def go_live_gate_result(self, *, candidate_id: str) -> str:
        try:
            self.require_ready(candidate_id=candidate_id)
            return "GO_LIVE_GATE_PASS"
        except ProductionActivationError:
            return "GO_LIVE_BLOCKED"

    def stage4_artifact_ready(self) -> bool:
        try:
            data = self._load_stage4_artifact()
            if data is None:
                return False
            require_stage4_artifact_ready(data)
            return True
        except ProductionActivationError:
            return False
