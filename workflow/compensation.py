"""Minimal compensation contract for committed side effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from workflow.models import utc_now


@dataclass(frozen=True)
class CompensationRecord:
    workflow_id: str
    step_id: str
    execution_id: str
    status: str
    attempted_at: datetime
    completed_at: datetime | None = None
    error_code: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata or {}))
        )


class CompensationHistory:
    """Append-only in-memory history; also mirrored into workflow metadata."""

    def __init__(self):
        self._items: list[CompensationRecord] = []

    def append(self, record: CompensationRecord) -> None:
        self._items.append(record)

    def for_workflow(self, workflow_id: str) -> tuple[CompensationRecord, ...]:
        return tuple(r for r in self._items if r.workflow_id == workflow_id)

    def record_success(
        self, *, workflow_id: str, step_id: str, execution_id: str, metadata=None
    ) -> CompensationRecord:
        now = utc_now()
        rec = CompensationRecord(
            workflow_id=workflow_id,
            step_id=step_id,
            execution_id=execution_id,
            status="compensated",
            attempted_at=now,
            completed_at=now,
            metadata=metadata or {},
        )
        self.append(rec)
        return rec

    def record_failure(
        self,
        *,
        workflow_id: str,
        step_id: str,
        execution_id: str,
        error_code: str,
        metadata=None,
    ) -> CompensationRecord:
        now = utc_now()
        rec = CompensationRecord(
            workflow_id=workflow_id,
            step_id=step_id,
            execution_id=execution_id,
            status="compensation_failed",
            attempted_at=now,
            completed_at=now,
            error_code=error_code,
            metadata=metadata or {},
        )
        self.append(rec)
        return rec
