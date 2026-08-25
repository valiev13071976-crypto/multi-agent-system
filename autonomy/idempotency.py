from dataclasses import replace

from autonomy.errors import IdempotencyConflictError
from autonomy.models import (
    IDEMPOTENCY_ACTIVE,
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_FAILED,
    IDEMPOTENCY_RESERVED,
    IDEMPOTENCY_STARTED,
    IdempotencyRecord,
    sanitize_metadata,
    utc_now,
)
from autonomy.store import IdempotencyStore, InMemoryIdempotencyStore


class IdempotencyRegistry:
    """In-memory foundation only. Not a distributed safety claim."""

    def __init__(self, store: IdempotencyStore | None = None):
        self.store = store or InMemoryIdempotencyStore()

    def get(self, key: str) -> IdempotencyRecord | None:
        return self.store.get(key)

    def reserve(self, key: str, action_id: str, metadata=None) -> IdempotencyRecord:
        existing = self.store.get(key)
        now = utc_now()
        if existing is not None:
            if existing.state == IDEMPOTENCY_COMPLETED:
                raise IdempotencyConflictError(key, "duplicate_completed")
            if existing.state in IDEMPOTENCY_ACTIVE:
                raise IdempotencyConflictError(key, "duplicate_active")
            if existing.state == IDEMPOTENCY_FAILED:
                raise IdempotencyConflictError(key, "duplicate_failed")
            raise IdempotencyConflictError(key, "duplicate_execution")
        record = IdempotencyRecord(
            key=key,
            action_id=action_id,
            state=IDEMPOTENCY_RESERVED,
            created_at=now,
            updated_at=now,
            metadata=sanitize_metadata(metadata),
        )
        self.store.put(record)
        return record

    def mark_started(self, key: str) -> IdempotencyRecord:
        return self._set_state(key, IDEMPOTENCY_STARTED)

    def mark_completed(self, key: str) -> IdempotencyRecord:
        return self._set_state(key, IDEMPOTENCY_COMPLETED)

    def mark_failed(self, key: str) -> IdempotencyRecord:
        return self._set_state(key, IDEMPOTENCY_FAILED)

    def _set_state(self, key: str, state: str) -> IdempotencyRecord:
        existing = self.store.get(key)
        if existing is None:
            raise KeyError(key)
        updated = replace(existing, state=state, updated_at=utc_now())
        self.store.put(updated)
        return updated
