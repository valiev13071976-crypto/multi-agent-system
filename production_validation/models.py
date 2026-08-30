"""Release evidence and gate result models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    LOCAL_FIXTURE = "LOCAL_FIXTURE"
    PRODUCTION_LIKE = "PRODUCTION_LIKE"
    LIVE_SAFE = "LIVE_SAFE"
    LIVE_MUTATING = "LIVE_MUTATING"


class GateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    SKIP = "SKIP"


class VerificationClass(str, Enum):
    CODE_VERIFIED = "CODE_VERIFIED"
    CONFIG_VERIFIED = "CONFIG_VERIFIED"
    LIVE_VERIFIED = "LIVE_VERIFIED"
    OPERATOR_ACTION_REQUIRED = "OPERATOR_ACTION_REQUIRED"
    NOT_ENABLED = "NOT_ENABLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReleaseEvidence:
    evidence_id: str
    stage: str
    gate: str
    environment: str
    classification: str
    mode: str
    status: str
    started_at: str
    completed_at: str = ""
    safe_metrics: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    artifact_ref: str = ""
    failure_category: str = ""
    operator_action: str = ""
    release_identity: str = ""
    superseded_by: str = ""

    @classmethod
    def begin(
        cls,
        *,
        gate: str,
        environment: str,
        mode: ExecutionMode,
        classification: str = VerificationClass.CODE_VERIFIED.value,
        release_identity: str = "",
    ) -> ReleaseEvidence:
        return cls(
            evidence_id=f"ev-{uuid.uuid4().hex[:16]}",
            stage="stage3",
            gate=gate,
            environment=environment,
            classification=classification,
            mode=mode.value if hasattr(mode, "value") else str(mode),
            status="running",
            started_at=_utc(),
            correlation_id=uuid.uuid4().hex[:12],
            release_identity=release_identity,
        )

    def complete(
        self,
        *,
        status: GateStatus,
        classification: str | None = None,
        safe_metrics: dict[str, Any] | None = None,
        artifact_ref: str = "",
        failure_category: str = "",
        operator_action: str = "",
    ) -> ReleaseEvidence:
        self.status = status.value
        self.completed_at = _utc()
        if classification:
            self.classification = classification
        if safe_metrics:
            self.safe_metrics = dict(safe_metrics)
        self.artifact_ref = artifact_ref
        self.failure_category = failure_category
        self.operator_action = operator_action
        return self

    def as_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "stage": self.stage,
            "gate": self.gate,
            "environment": self.environment,
            "classification": self.classification,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "safe_metrics": dict(self.safe_metrics),
            "correlation_id": self.correlation_id,
            "artifact_ref": self.artifact_ref,
            "failure_category": self.failure_category,
            "operator_action": self.operator_action,
            "release_identity": self.release_identity,
            "superseded_by": self.superseded_by,
        }


@dataclass
class GateResult:
    gate: str
    status: GateStatus
    classification: str
    evidence_id: str = ""
    operator_action: str = ""
    safe_metrics: dict[str, Any] = field(default_factory=dict)
