"""Scheduled acquisition — uses WorkflowScheduler, no second scheduler."""

from __future__ import annotations

from datetime import datetime, timedelta

from acquisition.models import utc_now
from security.tenant import normalize_tenant_id
from workflow.definition import ScheduleSpec
from workflow.schedule import ScheduleState, WorkflowScheduler


WORKFLOW_TYPE_ACQUISITION = "acquisition.refresh"


def build_acquisition_schedule_spec(
    *,
    schedule_id: str,
    source_id: str,
    tenant_id: str,
    interval_seconds: float,
    acquisition_type: str = "http_get",
    target: str = "",
    version: str = "1.0.0",
    run_at: datetime | None = None,
) -> ScheduleSpec:
    return ScheduleSpec(
        schedule_id=schedule_id,
        workflow_type=WORKFLOW_TYPE_ACQUISITION,
        version=version,
        payload={
            "source_id": source_id,
            "tenant_id": normalize_tenant_id(tenant_id),
            "acquisition_type": acquisition_type,
            "target": target,
        },
        interval_seconds=float(interval_seconds),
        run_at=run_at,
    )


class AcquisitionScheduler:
    """Thin wrapper over WorkflowScheduler for source refresh jobs."""

    def __init__(self, workflow_scheduler: WorkflowScheduler | None = None):
        self.scheduler = workflow_scheduler or WorkflowScheduler()

    def register_source_refresh(
        self,
        *,
        schedule_id: str,
        source_id: str,
        tenant_id: str,
        interval_seconds: float,
        acquisition_type: str = "http_get",
        target: str = "",
    ) -> ScheduleState:
        spec = build_acquisition_schedule_spec(
            schedule_id=schedule_id,
            source_id=source_id,
            tenant_id=tenant_id,
            interval_seconds=interval_seconds,
            acquisition_type=acquisition_type,
            target=target,
        )
        return self.scheduler.register(spec)

    def due(self, now: datetime | None = None) -> tuple[ScheduleState, ...]:
        return self.scheduler.store.list_due(now or utc_now())

    def mark_enqueued(self, schedule_id: str, *, execution_key: str, now: datetime | None = None) -> ScheduleState:
        stamp = now or utc_now()
        state = self.scheduler.store.get(schedule_id)
        if state is None:
            raise KeyError(schedule_id)
        # Advance next_run for interval schedules (idempotent enqueue key)
        next_run = stamp
        if state.interval_seconds:
            next_run = stamp + timedelta(seconds=float(state.interval_seconds))
        from dataclasses import replace

        updated = replace(
            state,
            last_enqueued_at=stamp,
            last_execution_key=execution_key,
            next_run_at=next_run,
            run_count=state.run_count + 1,
        )
        self.scheduler.store.save(updated)
        return updated

    def execution_key(self, state: ScheduleState) -> str:
        """Idempotent key per schedule slot — prevents duplicate runs."""
        slot = state.next_run_at.isoformat()
        tenant = str(dict(state.payload).get("tenant_id") or "legacy-default")
        return f"acq:{tenant}:{state.schedule_id}:{slot}"
