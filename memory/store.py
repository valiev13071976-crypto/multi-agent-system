"""Memory store abstraction + in-memory implementation."""

from __future__ import annotations

import threading
from datetime import datetime

from memory.models import (
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_EXPIRED,
    MemoryLink,
    MemoryProvenance,
    MemoryRecord,
    MemoryScope,
    utc_now,
)


class MemoryVersionConflict(RuntimeError):
    def __init__(self, reason: str = "memory_version_conflict"):
        self.reason = reason
        super().__init__(reason)


class MemoryPersistenceUnavailableError(RuntimeError):
    def __init__(self, reason: str = "memory_persistence_unavailable"):
        self.reason = reason
        super().__init__(reason)


class MemoryStore:
    def create(self, record: MemoryRecord, provenance: MemoryProvenance, tags: tuple[str, ...] = ()) -> MemoryRecord:
        raise NotImplementedError

    def get(self, memory_id: str, *, scope: MemoryScope | None = None) -> MemoryRecord | None:
        raise NotImplementedError

    def update(self, record: MemoryRecord, *, expected_version: int) -> MemoryRecord:
        raise NotImplementedError

    def delete(self, memory_id: str, *, expected_version: int | None = None, scope: MemoryScope | None = None) -> MemoryRecord:
        raise NotImplementedError

    def list_by_scope(self, scope: MemoryScope, *, statuses: tuple[str, ...] | None = None) -> tuple[MemoryRecord, ...]:
        raise NotImplementedError

    def find_by_hash(self, scope: MemoryScope, memory_type: str, content_hash: str) -> MemoryRecord | None:
        raise NotImplementedError

    def find_active(self, scope: MemoryScope, memory_type: str | None = None) -> tuple[MemoryRecord, ...]:
        raise NotImplementedError

    def expire(self, memory_id: str, *, now: datetime | None = None, scope: MemoryScope | None = None) -> MemoryRecord:
        raise NotImplementedError

    def link(self, link: MemoryLink) -> MemoryLink:
        raise NotImplementedError

    def list_links(self, memory_id: str) -> tuple[MemoryLink, ...]:
        raise NotImplementedError

    def get_provenance(self, memory_id: str) -> MemoryProvenance | None:
        raise NotImplementedError

    def close(self) -> None:
        return None


class InMemoryMemoryStore(MemoryStore):
    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[str, MemoryRecord] = {}
        self._provenance: dict[str, MemoryProvenance] = {}
        self._links: list[MemoryLink] = []
        self.available = True
        self.connection_mode = "memory"
        self.persistence_backend = "memory"
        self.fts_available = False

    def create(self, record: MemoryRecord, provenance: MemoryProvenance, tags: tuple[str, ...] = ()) -> MemoryRecord:
        if not self.available:
            raise MemoryPersistenceUnavailableError()
        with self._lock:
            existing = self.find_by_hash(record.scope, record.memory_type, record.content_hash)
            if existing is not None and existing.status == STATUS_ACTIVE:
                return existing
            if record.memory_id in self._records:
                raise MemoryVersionConflict("memory_exists")
            tagged = record if not tags else _with_tags(record, tags)
            self._records[tagged.memory_id] = tagged
            self._provenance[tagged.memory_id] = provenance
            return tagged

    def get(self, memory_id: str, *, scope: MemoryScope | None = None) -> MemoryRecord | None:
        with self._lock:
            row = self._records.get(memory_id)
            if row is None:
                return None
            if scope is not None and row.scope.key() != scope.key():
                return None
            return row

    def update(self, record: MemoryRecord, *, expected_version: int) -> MemoryRecord:
        if not self.available:
            raise MemoryPersistenceUnavailableError()
        with self._lock:
            current = self._records.get(record.memory_id)
            if current is None:
                raise MemoryVersionConflict("memory_not_found")
            if current.version != expected_version:
                raise MemoryVersionConflict()
            updated = _clone(record, version=current.version + 1, updated_at=record.updated_at or utc_now())
            self._records[updated.memory_id] = updated
            return updated

    def delete(self, memory_id: str, *, expected_version: int | None = None, scope: MemoryScope | None = None) -> MemoryRecord:
        if not self.available:
            raise MemoryPersistenceUnavailableError()
        with self._lock:
            current = self._records.get(memory_id)
            if current is None:
                raise MemoryVersionConflict("memory_not_found")
            if scope is not None and current.scope.key() != scope.key():
                raise MemoryVersionConflict("memory_not_found")
            if expected_version is not None and current.version != expected_version:
                raise MemoryVersionConflict()
            tombstone = _clone(
                current,
                status=STATUS_DELETED,
                content_safe=None,
                encrypted_content=None,
                summary_safe=None,
                version=current.version + 1,
                updated_at=utc_now(),
            )
            self._records[memory_id] = tombstone
            return tombstone

    def list_by_scope(self, scope: MemoryScope, *, statuses: tuple[str, ...] | None = None) -> tuple[MemoryRecord, ...]:
        allowed = set(statuses or (STATUS_ACTIVE,))
        with self._lock:
            rows = [
                r
                for r in self._records.values()
                if r.scope.key() == scope.key() and r.status in allowed
            ]
        return tuple(sorted(rows, key=lambda r: r.memory_id))

    def find_by_hash(self, scope: MemoryScope, memory_type: str, content_hash: str) -> MemoryRecord | None:
        with self._lock:
            for row in self._records.values():
                if (
                    row.scope.key() == scope.key()
                    and row.memory_type == memory_type
                    and row.content_hash == content_hash
                    and row.status == STATUS_ACTIVE
                ):
                    return row
        return None

    def find_active(self, scope: MemoryScope, memory_type: str | None = None) -> tuple[MemoryRecord, ...]:
        with self._lock:
            rows = [
                r
                for r in self._records.values()
                if r.scope.key() == scope.key()
                and r.status == STATUS_ACTIVE
                and (memory_type is None or r.memory_type == memory_type)
            ]
        return tuple(sorted(rows, key=lambda r: r.memory_id))

    def expire(self, memory_id: str, *, now: datetime | None = None, scope: MemoryScope | None = None) -> MemoryRecord:
        stamp = now or utc_now()
        with self._lock:
            current = self._records.get(memory_id)
            if current is None:
                raise MemoryVersionConflict("memory_not_found")
            if scope is not None and current.scope.key() != scope.key():
                raise MemoryVersionConflict("memory_not_found")
            updated = _clone(
                current,
                status=STATUS_EXPIRED,
                version=current.version + 1,
                updated_at=stamp,
            )
            self._records[memory_id] = updated
            return updated

    def link(self, link: MemoryLink) -> MemoryLink:
        with self._lock:
            self._links.append(link)
            return link

    def list_links(self, memory_id: str) -> tuple[MemoryLink, ...]:
        with self._lock:
            return tuple(
                L
                for L in self._links
                if L.from_memory_id == memory_id or L.to_memory_id == memory_id
            )

    def get_provenance(self, memory_id: str) -> MemoryProvenance | None:
        with self._lock:
            return self._provenance.get(memory_id)

    def _require(self, memory_id: str) -> MemoryRecord:
        row = self._records.get(memory_id)
        if row is None:
            raise MemoryVersionConflict("memory_not_found")
        return row


def _with_tags(record: MemoryRecord, tags: tuple[str, ...]) -> MemoryRecord:
    return _clone(record, tags=tuple(dict.fromkeys(tuple(record.tags) + tuple(tags))))


def _clone(record: MemoryRecord, **kwargs) -> MemoryRecord:
    fields = {
        "memory_id": record.memory_id,
        "memory_type": record.memory_type,
        "scope": record.scope,
        "content_hash": record.content_hash,
        "source_type": record.source_type,
        "source_ref": record.source_ref,
        "provenance": record.provenance,
        "sensitivity": record.sensitivity,
        "status": record.status,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "title": record.title,
        "content_safe": record.content_safe,
        "encrypted_content": record.encrypted_content,
        "summary_safe": record.summary_safe,
        "confidence": record.confidence,
        "tags": record.tags,
        "expires_at": record.expires_at,
        "version": record.version,
        "metadata_safe": dict(record.metadata_safe),
    }
    fields.update(kwargs)
    return MemoryRecord(**fields)
