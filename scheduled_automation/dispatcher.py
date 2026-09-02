"""Dispatch scheduled occurrences into governed execution paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from scheduled_automation.config import WORKFLOW_TYPE_BUSINESS_AUTOMATION
from scheduled_automation.models import OCC_DISPATCHED, ScheduleOccurrence


DispatchFn = Callable[..., dict[str, Any]]


@dataclass
class DispatchResult:
    run_id: str
    status: str
    lane: str
    execution_key: str


@dataclass
class ScheduledAutomationDispatcher:
    dispatch_fn: DispatchFn | None = None
    dispatches: list[dict[str, Any]] = field(default_factory=list)

    def dispatch(self, *, tenant_id: str, occurrence: ScheduleOccurrence, schedule_payload: dict[str, Any]) -> DispatchResult:
        execution_key = occurrence.execution_key
        lane = str(schedule_payload.get("workload_class") or "BACKGROUND").lower()
        metadata = {
            "trigger": "scheduled",
            "schedule_id": occurrence.schedule_id,
            "occurrence_id": occurrence.occurrence_id,
            "schedule_version": occurrence.schedule_version,
            "execution_lane": "scheduled",
            "tenant_id": tenant_id,
            **schedule_payload,
        }
        if self.dispatch_fn is None:
            run_id = f"run-{occurrence.occurrence_id}"
            out = {"run_id": run_id, "workflow_type": WORKFLOW_TYPE_BUSINESS_AUTOMATION, "metadata": metadata}
        else:
            out = self.dispatch_fn(
                workflow_type=WORKFLOW_TYPE_BUSINESS_AUTOMATION,
                version="1",
                execution_key=execution_key,
                tenant_id=tenant_id,
                metadata=metadata,
                execution_lane="scheduled",
            )
        record = {
            "tenant_id": tenant_id,
            "occurrence_id": occurrence.occurrence_id,
            "execution_key": execution_key,
            "result": out,
        }
        self.dispatches.append(record)
        return DispatchResult(
            run_id=str(out.get("run_id") or out.get("workflow_id") or ""),
            status=OCC_DISPATCHED,
            lane=lane,
            execution_key=execution_key,
        )
