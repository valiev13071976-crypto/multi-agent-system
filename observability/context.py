"""Immutable observability context: correlation / trace / span lineage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ObservabilityContext:
    correlation_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    workflow_id: str = ""
    task_id: str = ""
    actor_ref: str = ""
    tenant_id: str = ""
    started_at: datetime | None = None

    def __post_init__(self):
        if self.started_at is None:
            object.__setattr__(self, "started_at", _utc_now())
        elif self.started_at.tzinfo is None:
            object.__setattr__(
                self, "started_at", self.started_at.replace(tzinfo=timezone.utc)
            )

    @classmethod
    def root(
        cls,
        *,
        correlation_id: str | None = None,
        workflow_id: str = "",
        task_id: str = "",
        actor_ref: str = "",
        tenant_id: str = "",
        started_at: datetime | None = None,
    ) -> ObservabilityContext:
        cid = correlation_id or _new_id()
        tid = _new_id()
        return cls(
            correlation_id=cid,
            trace_id=tid,
            span_id=_new_id(),
            parent_span_id=None,
            workflow_id=workflow_id,
            task_id=task_id,
            actor_ref=actor_ref,
            tenant_id=tenant_id,
            started_at=started_at or _utc_now(),
        )

    def child(
        self,
        *,
        workflow_id: str | None = None,
        task_id: str | None = None,
        actor_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> ObservabilityContext:
        return replace(
            self,
            span_id=_new_id(),
            parent_span_id=self.span_id,
            workflow_id=self.workflow_id if workflow_id is None else workflow_id,
            task_id=self.task_id if task_id is None else task_id,
            actor_ref=self.actor_ref if actor_ref is None else actor_ref,
            tenant_id=self.tenant_id if tenant_id is None else tenant_id,
            started_at=_utc_now(),
        )

    def with_workflow(self, workflow_id: str) -> ObservabilityContext:
        return replace(self, workflow_id=str(workflow_id or ""))

    def with_task(self, task_id: str) -> ObservabilityContext:
        return replace(self, task_id=str(task_id or ""))

    def with_tenant(self, tenant_id: str) -> ObservabilityContext:
        return replace(self, tenant_id=str(tenant_id or ""))
