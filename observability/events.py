"""Operational event model and in-memory sink."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from observability.security import sanitize_observability_metadata


EVENT_TYPES = frozenset(
    {
        "workflow.created",
        "workflow.started",
        "workflow.waiting_approval",
        "workflow.resumed",
        "workflow.completed",
        "workflow.failed",
        "workflow.cancelled",
        "tool.requested",
        "tool.denied",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "tool.uncertain",
        "autonomy.evaluated",
        "hitl.requested",
        "hitl.approved",
        "hitl.rejected",
        "hitl.expired",
        "hitl.cancelled",
        "permit.issued",
        "permit.consumed",
        "permit.denied",
        "permit.expired",
        "side_effect.prepared",
        "side_effect.started",
        "side_effect.completed",
        "side_effect.failed",
        "side_effect.uncertain",
        "reconciliation.created",
        "reconciliation.checked",
        "reconciliation.completed",
        "reconciliation.manual_review",
        "queue.enqueued",
        "queue.started",
        "queue.completed",
        "queue.failed",
        "queue.dead_lettered",
        "queue.cancelled",
        "provider.selected",
        "provider.completed",
        "provider.failed",
        "validation.completed",
        "judge.completed",
        "finops.recorded",
        "finops.budget_denied",
        "finops.unknown_cost",
        "budget.evaluated",
        "budget.reserved",
        "budget.reconciled",
        "budget.released",
        "budget.degraded",
        "budget.terminated",
        "budget.forecasted",
        "recovery.case_created",
        "recovery.queued",
        "recovery.check_started",
        "recovery.check_completed",
        "recovery.waiting_operator",
        "recovery.decision_recorded",
        "recovery.resolved",
        "recovery.blocked",
        "recovery.failed",
        "memory.ingested",
        "memory.deduplicated",
        "memory.retrieved",
        "memory.updated",
        "memory.superseded",
        "memory.forgotten",
        "memory.expired",
        "memory.denied",
        "document.ingested",
        "document.parsed",
        "document.partial",
        "document.failed",
        "document.chunked",
        "document.deduplicated",
        "document.deleted",
        "document.denied",
        "spreadsheet.inspected",
        "spreadsheet.range_extracted",
        "knowledge.source_registered",
        "knowledge.ingested",
        "knowledge.retrieved",
        "knowledge.refresh_started",
        "knowledge.refresh_completed",
        "knowledge.refresh_failed",
        "knowledge.stale",
        "knowledge.denied",
        "knowledge.conflict_detected",
        "procurement.request_created",
        "procurement.requirements_normalized",
        "procurement.research_started",
        "procurement.suppliers_found",
        "procurement.offers_normalized",
        "procurement.offer_rejected",
        "procurement.comparison_completed",
        "procurement.risk_detected",
        "procurement.recommendation_created",
        "procurement.approval_requested",
        "procurement.approved",
        "procurement.rejected",
        "procurement.completed",
        "procurement.failed",
    }
)


@dataclass(frozen=True)
class OperationalEvent:
    event_id: str
    event_type: str
    timestamp: datetime
    correlation_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    workflow_id: str = ""
    task_id: str = ""
    tool_id: str = ""
    operation: str = ""
    provider: str = ""
    model: str = ""
    component: str = ""
    status: str = ""
    duration_ms: int | None = None
    error_code: str | None = None
    risk: str = ""
    trust_level: str = ""
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    exception_type: str | None = None

    def __post_init__(self):
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown_event_type:{self.event_type}")
        stamp = self.timestamp
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
            object.__setattr__(self, "timestamp", stamp)
        object.__setattr__(
            self,
            "metadata_safe",
            MappingProxyType(dict(self.metadata_safe or {})),
        )


def make_event(
    event_type: str,
    *,
    correlation_id: str,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None = None,
    workflow_id: str = "",
    task_id: str = "",
    tool_id: str = "",
    operation: str = "",
    provider: str = "",
    model: str = "",
    component: str = "",
    status: str = "",
    duration_ms: int | None = None,
    error_code: str | None = None,
    risk: str = "",
    trust_level: str = "",
    metadata: Mapping | None = None,
    exception_type: str | None = None,
    timestamp: datetime | None = None,
    max_bytes: int = 4096,
) -> OperationalEvent:
    cleaned, truncated = sanitize_observability_metadata(metadata, max_bytes=max_bytes)
    if truncated:
        cleaned = dict(cleaned)
        cleaned["metadata_truncated"] = True
    return OperationalEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        timestamp=timestamp or datetime.now(timezone.utc),
        correlation_id=correlation_id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        workflow_id=workflow_id,
        task_id=task_id,
        tool_id=tool_id,
        operation=operation,
        provider=provider,
        model=model,
        component=component,
        status=status,
        duration_ms=duration_ms,
        error_code=error_code,
        risk=risk,
        trust_level=trust_level,
        metadata_safe=cleaned,
        exception_type=exception_type,
    )


@runtime_checkable
class ObservabilitySink(Protocol):
    def emit(self, event: OperationalEvent) -> None: ...


class InMemoryObservabilitySink:
    """Bounded FIFO buffer. Emission never reaches the network."""

    def __init__(self, max_events: int = 1000):
        self._max = max(1, int(max_events))
        self._events: deque[OperationalEvent] = deque(maxlen=self._max)
        self._lock = threading.Lock()
        self.emit_failures = 0

    def emit(self, event: OperationalEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> tuple[OperationalEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
