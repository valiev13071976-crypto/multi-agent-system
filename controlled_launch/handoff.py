"""Stage-3 handoff gate — fail-closed; durable artifact authoritative for historical closure."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from controlled_launch.errors import BLOCKED_BY_STAGE_3, STALE_STAGE3_HANDOFF, ControlledLaunchError
from controlled_launch.models import Stage3Handoff
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import GateStatus
from production_validation.release_gate import MANDATORY_LIVE_GATES
from production_validation.stage3_artifact import (
    Stage3HandoffArtifactError,
    load_stage3_handoff_artifact,
    require_stage3_artifact_ready,
)


@dataclass
class Stage3HandoffGate:
    """Read-only Stage-3 prerequisite for Controlled Launch / Stage 5.

    Historical Stage3 closure: durable STAGE3_HANDOFF.json (authoritative).
    Mutable EvidenceStore: current/future operational evidence only; not required
    to re-prove an already-closed Stage3 when the immutable artifact is valid.

    Does NOT invent LIVE_VERIFIED evidence. Does NOT activate GO LIVE.
    """

    config: ValidationConfig | None = None
    evidence_store: EvidenceStore | None = None
    stage3_artifact_path: str | Path | None = None
    require_stage3_artifact: bool = True

    def __post_init__(self):
        if self.config is None:
            self.config = ValidationConfig.from_env()
        if self.evidence_store is None:
            self.evidence_store = EvidenceStore()

    def _load_stage3_artifact(self) -> dict | None:
        if not self.require_stage3_artifact and self.stage3_artifact_path is None:
            return None
        try:
            return load_stage3_handoff_artifact(self.stage3_artifact_path)
        except Stage3HandoffArtifactError:
            if self.require_stage3_artifact:
                raise
            return None

    def _from_artifact(self, artifact: dict) -> Stage3Handoff:
        require_stage3_artifact_ready(artifact)
        # Do not bind Stage3 historical closure to current deploy SHA.
        identity = str(artifact.get("stage3_release_identity") or "")
        if identity.lower() in {"", "unavailable", "unknown"}:
            identity = ""
        evidence_id = str(artifact.get("evidence_id") or "")
        verified_at = str(artifact.get("closure_timestamp") or "")
        if verified_at.lower() in {"unavailable", "unknown"}:
            verified_at = ""
        return Stage3Handoff(
            evidence_id=evidence_id,
            stage3_status="CLOSED",
            release_readiness="READY",
            p0_count=0,
            p1_count=0,
            release_identity=identity,
            environment=self.config.environment,
            commit_sha=identity,
            deployment_id="",
            verified_at=verified_at,
            capacity_envelope={"authoritative_source": "stage3_handoff_artifact", "schema_version": artifact.get("schema_version")},
        )

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

    def _from_evidence_store(self) -> Stage3Handoff:
        gates, blocked = self._mandatory_status()
        latest_release = self.evidence_store.latest_for_gate("3.17_release_gate") or {}
        metrics = dict(latest_release.get("safe_metrics") or {})
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
            capacity_envelope={
                **dict(metrics.get("capacity_envelope") or {}),
                "mandatory_gates": gates,
                "blocked_gates": blocked,
                "authoritative_source": "mandatory_live_gates",
            },
        )

    def evaluate(self) -> Stage3Handoff:
        # Prefer immutable Stage3 handoff artifact for historical closure.
        try:
            artifact = self._load_stage3_artifact()
            if artifact is not None:
                return self._from_artifact(artifact)
        except Stage3HandoffArtifactError as exc:
            if self.require_stage3_artifact:
                # Fail closed: required artifact invalid/missing → OPEN
                return Stage3Handoff(
                    evidence_id="",
                    stage3_status="OPEN",
                    release_readiness="NOT_READY",
                    p0_count=0,
                    p1_count=0,
                    release_identity="",
                    environment=self.config.environment,
                    commit_sha="",
                    deployment_id="",
                    verified_at="",
                    capacity_envelope={"authoritative_source": "stage3_artifact_error", "error": exc.code, **exc.details},
                )
        return self._from_evidence_store()

    def require_ready(self, *, commit_sha: str = "", deployment_id: str = "", environment: str = "") -> Stage3Handoff:
        handoff = self.evaluate()
        if handoff.stage3_status != "CLOSED":
            raise ControlledLaunchError(BLOCKED_BY_STAGE_3, details={"stage3_status": handoff.stage3_status})
        if handoff.release_readiness != "READY":
            raise ControlledLaunchError(BLOCKED_BY_STAGE_3, details={"release_readiness": handoff.release_readiness})
        if handoff.p0_count > 0 or handoff.p1_count > 0:
            raise ControlledLaunchError(BLOCKED_BY_STAGE_3, details={"p0": handoff.p0_count, "p1": handoff.p1_count})
        # Historical Stage3 identity is independent of later Stage5 patch SHAs.
        # Only enforce identity match when BOTH sides have a concrete non-empty SHA.
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
