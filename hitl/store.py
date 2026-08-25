from autonomy.store import (
    ApprovalStore,
    InMemoryApprovalStore,
)
from hitl.models import ExecutionPermit, HITLAuditEvent, PERMIT_ISSUED


class ExecutionPermitStore:
    def create(self, permit: ExecutionPermit) -> None:
        raise NotImplementedError

    def get(self, permit_id: str) -> ExecutionPermit | None:
        raise NotImplementedError

    def save(self, permit: ExecutionPermit) -> None:
        raise NotImplementedError

    def find_active_by_approval(self, approval_id: str) -> ExecutionPermit | None:
        raise NotImplementedError


class HITLAuditStore:
    def append(self, event: HITLAuditEvent) -> None:
        raise NotImplementedError

    def list_all(self) -> tuple[HITLAuditEvent, ...]:
        raise NotImplementedError


class InMemoryExecutionPermitStore(ExecutionPermitStore):
    def __init__(self):
        self._items: dict[str, ExecutionPermit] = {}

    def create(self, permit: ExecutionPermit) -> None:
        self._items[permit.permit_id] = permit

    def get(self, permit_id: str) -> ExecutionPermit | None:
        return self._items.get(permit_id)

    def save(self, permit: ExecutionPermit) -> None:
        self._items[permit.permit_id] = permit

    def find_active_by_approval(self, approval_id: str) -> ExecutionPermit | None:
        for item in self._items.values():
            if item.approval_id == approval_id and item.status == PERMIT_ISSUED:
                return item
        return None


class InMemoryHITLAuditStore(HITLAuditStore):
    def __init__(self):
        self._events: list[HITLAuditEvent] = []

    def append(self, event: HITLAuditEvent) -> None:
        self._events.append(event)

    def list_all(self) -> tuple[HITLAuditEvent, ...]:
        return tuple(self._events)


# Shared P5A store backend; HITL does not create a second approval database.
HITLApprovalStore = ApprovalStore
InMemoryHITLApprovalStore = InMemoryApprovalStore
