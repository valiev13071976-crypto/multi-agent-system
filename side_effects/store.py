from typing import Protocol

from side_effects.errors import SideEffectExecutionDeniedError
from side_effects.models import SideEffectExecutionRecord


class SideEffectExecutionStore(Protocol):
    def create(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord: ...

    def get(self, execution_id: str) -> SideEffectExecutionRecord | None: ...

    def get_for_tenant(
        self, execution_id: str, tenant_id: str
    ) -> SideEffectExecutionRecord | None: ...

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

    @staticmethod
    def _assert_tenant_writable(
        record: SideEffectExecutionRecord, *, existing: SideEffectExecutionRecord | None
    ) -> None:
        tid = str(record.tenant_id or "").strip()
        if tid:
            return
        if existing is not None and not str(existing.tenant_id or "").strip():
            return
        raise SideEffectExecutionDeniedError("side_effect_tenant_required")

    def create(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord:
        self._assert_tenant_writable(record, existing=None)
        self._records[record.execution_id] = record
        return record

    def get(self, execution_id: str) -> SideEffectExecutionRecord | None:
        return self._records.get(execution_id)

    def get_for_tenant(
        self, execution_id: str, tenant_id: str
    ) -> SideEffectExecutionRecord | None:
        tid = str(tenant_id or "").strip()
        if not tid:
            return None
        record = self.get(execution_id)
        if record is None:
            return None
        if str(record.tenant_id or "").strip() != tid:
            return None
        return record

    def save(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord:
        existing = self._records.get(record.execution_id)
        self._assert_tenant_writable(record, existing=existing)
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

    def list_by_tenant(self, tenant_id: str) -> tuple[SideEffectExecutionRecord, ...]:
        tid = str(tenant_id or "").strip()
        if not tid:
            return ()
        return tuple(
            row for row in self._records.values() if str(row.tenant_id or "") == tid
        )

    def list_all(self) -> tuple[SideEffectExecutionRecord, ...]:
        return tuple(self._records.values())
