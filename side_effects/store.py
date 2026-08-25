from typing import Protocol

from side_effects.models import SideEffectExecutionRecord


class SideEffectExecutionStore(Protocol):
    def create(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord: ...

    def get(self, execution_id: str) -> SideEffectExecutionRecord | None: ...

    def save(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord: ...

    def find_by_action(self, action_id: str) -> tuple[SideEffectExecutionRecord, ...]: ...

    def find_by_idempotency(
        self, idempotency_key_hash: str
    ) -> SideEffectExecutionRecord | None: ...

    def list_by_workflow(
        self, workflow_id: str
    ) -> tuple[SideEffectExecutionRecord, ...]: ...


class InMemorySideEffectExecutionStore:
    """Ephemeral metadata only. Persistent backends must use EncryptionService."""

    def __init__(self):
        self._records: dict[str, SideEffectExecutionRecord] = {}

    def create(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord:
        self._records[record.execution_id] = record
        return record

    def get(self, execution_id: str) -> SideEffectExecutionRecord | None:
        return self._records.get(execution_id)

    def save(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord:
        self._records[record.execution_id] = record
        return record

    def find_by_action(self, action_id: str) -> tuple[SideEffectExecutionRecord, ...]:
        return tuple(
            row for row in self._records.values() if row.action_id == action_id
        )

    def find_by_idempotency(
        self, idempotency_key_hash: str
    ) -> SideEffectExecutionRecord | None:
        for row in self._records.values():
            if row.idempotency_key_hash == idempotency_key_hash:
                return row
        return None

    def list_by_workflow(
        self, workflow_id: str
    ) -> tuple[SideEffectExecutionRecord, ...]:
        return tuple(
            row for row in self._records.values() if row.workflow_id == workflow_id
        )
