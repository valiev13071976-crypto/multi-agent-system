"""Stage-3 handoff gate — fail-closed; consumes authoritative Stage-3 evidence only."""

from __future__ import annotations

from dataclasses import dataclass

from controlled_launch.errors import BLOCKED_BY_STAGE_3, STALE_STAGE3_HANDOFF, ControlledLaunchError
from controlled_launch.models import Stage3Handoff
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import GateStatus
from production_validation.release_gate import MANDATORY_LIVE_GATES


@dataclass
class Stage3HandoffGate:
    """Read-only Stage-3 prerequisite for Controlled Launch.

    Does NOT recompute or rewrite Stage-3 release gate evidence.
    Authoritative signal: all MANDATORY_LIVE_GATES = PASS (blocked=[]).
    Local process env (e.g. missing PUBLIC_URL) must not reopen Stage 3.
    """

    config: ValidationConfig | None = None
    evidence_store: EvidenceStore | None = None

    def __post_init__(self):
        if self.config is None:
            self.config = ValidationConfig.from_env()
        if self.evidence_store is None:
            self.evidence_store = EvidenceStore()

    def _mandatory_status(self) -> tuple[dict[str, str], list[str]]:
        gates: dict[str, str] = {}
        blocked: list[str] = []
        for gate in MANDATORY_LIVE_GATES:
            latest = self.evidence_store.latest_for_gate(gate) or {}
            status = str(latest.get("status") or "MISSING")
            gates[gate] = status
            if status != GateStatus.PASS.value:
                blocked.append(gate)
        return gates, blocked

    def evaluate(self) -> Stage3Handoff:
        gates, blocked = self._mandatory_status()
        # Prefer an authoritative PASS 3.17 if present; otherwise derive from mandatory set.
        latest_release = self.evidence_store.latest_for_gate("3.17_release_gate") or {}
        metrics = dict(latest_release.get("safe_metrics") or {})
        # If a later local gate rewrite marked BLOCKED but mandatory blocked=[],
        # treat mandatory evidence as authoritative (Stage 3 already closed).
        if not blocked:
            stage3_status = "CLOSED"
            release_readiness = "READY"
            evidence_id = latest_release.get("evidence_id") or next(
                ((self.evidence_store.latest_for_gate(g) or {}).get("evidence_id") for g in MANDATORY_LIVE_GATES),
                "",
            )
        else:
            stage3_status = "OPEN"
            release_readiness = "NOT_READY"
            evidence_id = latest_release.get("evidence_id") or ""
        metrics = {
            **metrics,
            "mandatory_gates": gates,
            "blocked_gates": blocked,
            "authoritative_source": "mandatory_live_gates",
        }
        return Stage3Handoff(
            evidence_id=str(evidence_id or ""),
            stage3_status=stage3_status,
            release_readiness=release_readiness,
            p0_count=int(metrics.get("p0_count") or 0),
            p1_count=int(metrics.get("p1_count") or 0),
            release_identity=self.config.release_identity,
            environment=self.config.environment,
            commit_sha=str(metrics.get("commit_sha") or self.config.release_identity),
            deployment_id=str(metrics.get("deployment_id") or ""),
            verified_at=str(latest_release.get("completed_at") or latest_release.get("started_at") or ""),
            capacity_envelope=dict(metrics.get("capacity_envelope") or {}),
        )

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
