"""E2E evidence and scenario result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class E2EEvidence:
    scenario_id: str
    tenant_id: str
    status: str
    started_at: str
    completed_at: str = ""
    workflow_id: str | None = None
    execution_id: str | None = None
    run_id: str | None = None
    schedule_id: str | None = None
    occurrence_id: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    business_result: dict[str, Any] = field(default_factory=dict)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    audit_refs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fixture_mode: bool = True
    live_active: bool = False

    def finalize(self, *, status: str) -> "E2EEvidence":
        self.status = status
        self.completed_at = utc_now_iso()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "workflow_id": self.workflow_id,
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "schedule_id": self.schedule_id,
            "occurrence_id": self.occurrence_id,
            "steps": self.steps,
            "business_result": self.business_result,
            "side_effects": self.side_effects,
            "approvals": self.approvals,
            "audit_refs": self.audit_refs,
            "errors": self.errors,
            "fixture_mode": self.fixture_mode,
            "live_active": self.live_active,
        }


@dataclass
class E2EWorld:
    activation: Any
    ba: Any
    analytics: Any
    scheduling: Any
    tenants: tuple[str, str] = ("tenant-a", "tenant-b")
