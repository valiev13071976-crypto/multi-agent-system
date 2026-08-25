import uuid

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata, utc_now


@dataclass(frozen=True)
class SideEffectAuditEvent:
    event_id: str
    event_type: str
    timestamp: datetime
    execution_id: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    action_id: str | None = None
    tool_id: str | None = None
    operation: str | None = None
    authorization_type: str | None = None
    authorization_id: str | None = None
    reason_code: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self, "metadata", MappingProxyType(sanitize_metadata(self.metadata))
        )


class SideEffectAuditLog:
    def __init__(self):
        self._events: list[SideEffectAuditEvent] = []

    def record(
        self,
        event_type: str,
        *,
        execution_id: str | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        tool_id: str | None = None,
        operation: str | None = None,
        authorization_type: str | None = None,
        authorization_id: str | None = None,
        reason_code: str | None = None,
        metadata=None,
    ) -> SideEffectAuditEvent:
        event = SideEffectAuditEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            timestamp=utc_now(),
            execution_id=execution_id,
            workflow_id=workflow_id,
            task_id=task_id,
            action_id=action_id,
            tool_id=tool_id,
            operation=operation,
            authorization_type=authorization_type,
            authorization_id=authorization_id,
            reason_code=reason_code,
            metadata=sanitize_metadata(metadata),
        )
        self._events.append(event)
        return event

    def events(self) -> tuple[SideEffectAuditEvent, ...]:
        return tuple(self._events)
