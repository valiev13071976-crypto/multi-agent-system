"""Stage-5 handoff gate — fail-closed on Stage 3 + Stage 4."""

from __future__ import annotations

from dataclasses import dataclass

from controlled_launch.handoff import Stage3HandoffGate
from controlled_launch.models import CandidateStatus, PromotionResult
from controlled_launch.promotion import PromotionGate
from controlled_launch.sqlite_store import SqliteControlledLaunchStore
from production_activation.errors import BLOCKED_BY_PREVIOUS_STAGE, STALE_HANDOFF, ProductionActivationError
from production_activation.models import Stage5Handoff


@dataclass
class Stage5HandoffGate:
    stage3_gate: Stage3HandoffGate | None = None
    launch_store: SqliteControlledLaunchStore | None = None

    def __post_init__(self):
        if self.stage3_gate is None:
            self.stage3_gate = Stage3HandoffGate()
        if self.launch_store is None:
            self.launch_store = SqliteControlledLaunchStore()

    def _stage4_handoff(self, candidate_id: str) -> dict:
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
        stage4_status = "CLOSED" if candidate.status == CandidateStatus.GO_LIVE_ELIGIBLE.value and promotion.result == PromotionResult.GO_LIVE_ELIGIBLE.value else "OPEN"
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
        s4 = self._stage4_handoff(candidate_id)
        candidate = s4.get("candidate")
        return Stage5Handoff(
            stage3_status=s3.stage3_status,
            stage3_readiness=s3.release_readiness,
            stage3_p0=s3.p0_count,
            stage3_p1=s3.p1_count,
            stage3_evidence_id=s3.evidence_id,
            stage4_status=s4["status"],
            promotion_decision=s4["promotion"],
            stage4_p0=s4["p0"],
            stage4_p1=s4["p1"],
            stage4_evidence_id=s4["evidence_id"],
            candidate_id=candidate_id,
            commit_sha=getattr(candidate, "commit_sha", "") if candidate else "",
            deployment_id=getattr(candidate, "deployment_id", "") if candidate else "",
            environment=getattr(candidate, "environment", "") if candidate else "",
            rollback_target=getattr(candidate, "rollback_target", "") if candidate else "",
            monitoring_ready=bool(getattr(candidate, "capacity_envelope", {}).get("monitoring_ready")) if candidate else False,
            alerts_ready=bool(getattr(candidate, "capacity_envelope", {}).get("alerts_ready")) if candidate else False,
            backup_ready=bool(getattr(candidate, "capacity_envelope", {}).get("backup_ready")) if candidate else False,
        )

    def require_ready(
        self,
        *,
        candidate_id: str,
        commit_sha: str = "",
        deployment_id: str = "",
        environment: str = "",
    ) -> Stage5Handoff:
        handoff = self.evaluate(candidate_id=candidate_id)
        if handoff.stage3_status != "CLOSED" or handoff.stage3_readiness != "READY":
            raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"stage3": handoff.stage3_status})
        if handoff.stage3_p0 > 0 or handoff.stage3_p1 > 0:
            raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"stage3_p": (handoff.stage3_p0, handoff.stage3_p1)})
        if handoff.stage4_status != "CLOSED" or handoff.promotion_decision != PromotionResult.GO_LIVE_ELIGIBLE.value:
            raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"stage4": handoff.stage4_status, "promotion": handoff.promotion_decision})
        if handoff.stage4_p0 > 0 or handoff.stage4_p1 > 0:
            raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"stage4_p": (handoff.stage4_p0, handoff.stage4_p1)})
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
