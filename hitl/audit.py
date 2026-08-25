import uuid

from autonomy.models import sanitize_metadata, utc_now
from hitl.models import HITLAuditEvent
from hitl.store import HITLAuditStore, InMemoryHITLAuditStore


class HITLAuditLog:
    def __init__(self, store: HITLAuditStore | None = None):
        self.store = store or InMemoryHITLAuditStore()

    def record(
        self,
        event_type: str,
        *,
        workflow_id: str,
        task_id: str,
        action_id: str,
        approval_id: str | None = None,
        permit_id: str | None = None,
        actor_id: str | None = None,
        reason_code: str | None = None,
        metadata=None,
    ) -> HITLAuditEvent:
        event = HITLAuditEvent(
            event_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            task_id=task_id,
            action_id=action_id,
            event_type=event_type,
            timestamp=utc_now(),
            approval_id=approval_id,
            permit_id=permit_id,
            actor_id=actor_id,
            reason_code=reason_code,
            metadata=sanitize_metadata(metadata),
        )
        self.store.append(event)
        return event

    def events(self) -> tuple[HITLAuditEvent, ...]:
        return self.store.list_all()
