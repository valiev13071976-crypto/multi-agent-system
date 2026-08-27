"""Commerce reconciliation scheduling — reuses WorkflowScheduler, no second scheduler."""

from __future__ import annotations

from datetime import datetime, timedelta

from security.tenant import normalize_tenant_id
from workflow.definition import ScheduleSpec
from workflow.models import utc_now
from workflow.schedule import ScheduleState, WorkflowScheduler

WORKFLOW_TYPE_COMMERCE_RECONCILE = "commerce.reconcile"
DEFAULT_INTERVAL_SECONDS = 3600.0


def commerce_reconcile_execution_key(*, tenant_id: str, schedule_window: int) -> str:
    tenant = normalize_tenant_id(tenant_id)
    return f"commerce-reconcile:{tenant}:{int(schedule_window)}"


def build_commerce_reconcile_schedule_spec(
    *,
    tenant_id: str,
    interval_seconds: float,
    version: str = "1",
    run_at: datetime | None = None,
    schedule_id: str | None = None,
) -> ScheduleSpec:
    tenant = normalize_tenant_id(tenant_id)
    sid = schedule_id or f"commerce-reconcile:{tenant}"
    return ScheduleSpec(
        schedule_id=sid,
        workflow_type=WORKFLOW_TYPE_COMMERCE_RECONCILE,
        version=version,
        payload={
            "tenant_id": tenant,
            "trigger": "scheduled",
            "execution_key_prefix": "commerce-reconcile",
        },
        interval_seconds=float(interval_seconds),
        run_at=run_at,
        enabled=True,
    )


class CommerceReconciliationScheduler:
    """Thin wrapper: one interval schedule per tenant on the shared WorkflowScheduler."""

    def __init__(self, workflow_scheduler: WorkflowScheduler | None = None):
        self.scheduler = workflow_scheduler or WorkflowScheduler()

    def ensure_tenant_schedule(
        self,
        *,
        tenant_id: str,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        run_at: datetime | None = None,
    ) -> ScheduleState:
        spec = build_commerce_reconcile_schedule_spec(
            tenant_id=tenant_id,
            interval_seconds=interval_seconds,
            run_at=run_at,
        )
        existing = self.scheduler.store.get(spec.schedule_id)
        if existing is not None:
            return existing
        return self.scheduler.register(spec)

    def register_tenants(
        self,
        tenants: list[str] | tuple[str, ...],
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> list[ScheduleState]:
        out = []
        for tenant in tenants:
            tid = str(tenant or "").strip()
            if not tid:
                continue
            out.append(
                self.ensure_tenant_schedule(
                    tenant_id=tid, interval_seconds=interval_seconds
                )
            )
        return out

    def execution_key(self, state: ScheduleState) -> str:
        tenant = str(dict(state.payload).get("tenant_id") or "legacy-default")
        window = int(state.next_run_at.timestamp())
        return commerce_reconcile_execution_key(tenant_id=tenant, schedule_window=window)

    def due(self, now: datetime | None = None) -> tuple[ScheduleState, ...]:
        stamp = now or utc_now()
        return tuple(
            s
            for s in self.scheduler.due(stamp)
            if s.workflow_type == WORKFLOW_TYPE_COMMERCE_RECONCILE
        )
