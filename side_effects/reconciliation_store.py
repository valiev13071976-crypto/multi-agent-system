from typing import Protocol

from side_effects.models import (
    RECON_MANUAL_REVIEW,
    RECONCILIATION_ACTIVE,
    ReconciliationRecord,
)


class ReconciliationStore(Protocol):
    def create(self, record: ReconciliationRecord) -> ReconciliationRecord: ...

    def get(self, reconciliation_id: str) -> ReconciliationRecord | None: ...

    def save(self, record: ReconciliationRecord) -> ReconciliationRecord: ...

    def find_by_execution(self, execution_id: str) -> tuple[ReconciliationRecord, ...]: ...

    def list_pending(self) -> tuple[ReconciliationRecord, ...]: ...

    def list_manual_review(self) -> tuple[ReconciliationRecord, ...]: ...


class InMemoryReconciliationStore:
    """Ephemeral metadata only. Persistent backends must use EncryptionService."""

    def __init__(self):
        self._records: dict[str, ReconciliationRecord] = {}

    def create(self, record: ReconciliationRecord) -> ReconciliationRecord:
        self._records[record.reconciliation_id] = record
        return record

    def get(self, reconciliation_id: str) -> ReconciliationRecord | None:
        return self._records.get(reconciliation_id)

    def save(self, record: ReconciliationRecord) -> ReconciliationRecord:
        self._records[record.reconciliation_id] = record
        return record

    def find_by_execution(self, execution_id: str) -> tuple[ReconciliationRecord, ...]:
        return tuple(
            row
            for row in self._records.values()
            if row.execution_id == execution_id
        )

    def list_pending(self) -> tuple[ReconciliationRecord, ...]:
        return tuple(
            row
            for row in self._records.values()
            if row.status in RECONCILIATION_ACTIVE
        )

    def list_manual_review(self) -> tuple[ReconciliationRecord, ...]:
        return tuple(
            row
            for row in self._records.values()
            if row.status == RECON_MANUAL_REVIEW
        )
