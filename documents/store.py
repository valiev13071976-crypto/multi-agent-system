"""Document store abstraction + in-memory implementation."""

from __future__ import annotations

import threading
from datetime import datetime

from documents.errors import DOCUMENT_STORE_UNAVAILABLE, DocumentError
from documents.models import DocumentChunkRecord, DocumentProvenance, DocumentRecord, STATUS_DELETED
from memory.models import MemoryScope, utc_now


class DocumentVersionConflict(DocumentError):
    def __init__(self, reason: str = "document_version_conflict"):
        super().__init__(reason)


class DocumentStore:
    def create(
        self,
        record: DocumentRecord,
        provenance: DocumentProvenance,
        tags: tuple[str, ...] = (),
    ) -> DocumentRecord:
        raise NotImplementedError

    def get(self, document_id: str, *, scope: MemoryScope | None = None) -> DocumentRecord | None:
        raise NotImplementedError

    def update(self, record: DocumentRecord, *, expected_version: int) -> DocumentRecord:
        raise NotImplementedError

    def delete(self, document_id: str, *, expected_version: int | None = None, scope: MemoryScope | None = None) -> DocumentRecord:
        raise NotImplementedError

    def find_by_hash(self, scope: MemoryScope, content_hash: str) -> DocumentRecord | None:
        raise NotImplementedError

    def list_by_scope(self, scope: MemoryScope, *, statuses: tuple[str, ...] | None = None):
        raise NotImplementedError

    def save_chunks(self, document_id: str, chunks: tuple[DocumentChunkRecord, ...]) -> None:
        raise NotImplementedError

    def list_chunks(self, document_id: str, *, scope: MemoryScope | None = None) -> tuple[DocumentChunkRecord, ...]:
        raise NotImplementedError

    def get_provenance(self, document_id: str) -> DocumentProvenance | None:
        raise NotImplementedError

    def get_blob(self, document_id: str) -> bytes | None:
        return None

    def put_blob(self, document_id: str, data: bytes) -> None:
        return None

    def delete_blob(self, document_id: str) -> None:
        return None

    def save_extract_partial(self, document_id: str, batch_index: int, payload: dict) -> None:
        return None

    def list_extract_partials(self, document_id: str) -> dict[int, dict]:
        return {}

    def clear_extract_partials(self, document_id: str) -> None:
        return None

    def close(self) -> None:
        return None


def _clone(record: DocumentRecord, **kwargs) -> DocumentRecord:
    fields = {
        "document_id": record.document_id,
        "scope": record.scope,
        "filename_safe": record.filename_safe,
        "media_type": record.media_type,
        "document_type": record.document_type,
        "size_bytes": record.size_bytes,
        "content_hash": record.content_hash,
        "source_type": record.source_type,
        "source_ref": record.source_ref,
        "provenance": record.provenance,
        "sensitivity": record.sensitivity,
        "status": record.status,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "version": record.version,
        "metadata_safe": dict(record.metadata_safe),
        "page_count": record.page_count,
        "sheet_count": record.sheet_count,
        "chunk_count": record.chunk_count,
        "parser_version": record.parser_version,
        "title": record.title,
        "warnings": record.warnings,
    }
    fields.update(kwargs)
    return DocumentRecord(**fields)


class InMemoryDocumentStore(DocumentStore):
    def __init__(self):
        self._lock = threading.RLock()
        self._docs: dict[str, DocumentRecord] = {}
        self._prov: dict[str, DocumentProvenance] = {}
        self._chunks: dict[str, list[DocumentChunkRecord]] = {}
        self._tags: dict[str, tuple[str, ...]] = {}
        self._blobs: dict[str, bytes] = {}
        self._partials: dict[str, dict[int, dict]] = {}
        self.available = True
        self.connection_mode = "memory"
        self.persistence_backend = "memory"

    def create(self, record, provenance, tags=()):
        if not self.available:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        with self._lock:
            existing = self.find_by_hash(record.scope, record.content_hash)
            if existing is not None and existing.status not in {STATUS_DELETED, "failed"}:
                return existing
            self._docs[record.document_id] = record
            self._prov[record.document_id] = provenance
            self._tags[record.document_id] = tuple(tags)
            self._chunks.setdefault(record.document_id, [])
            return record

    def get(self, document_id: str, *, scope: MemoryScope | None = None):
        with self._lock:
            row = self._docs.get(document_id)
            if row is None:
                return None
            if scope is not None and row.scope.key() != scope.key():
                return None
            return row

    def update(self, record, *, expected_version: int):
        if not self.available:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        with self._lock:
            current = self._docs.get(record.document_id)
            if current is None or current.version != expected_version:
                raise DocumentVersionConflict()
            updated = _clone(record, version=current.version + 1, updated_at=utc_now())
            self._docs[updated.document_id] = updated
            return updated

    def delete(self, document_id: str, *, expected_version: int | None = None, scope: MemoryScope | None = None):
        current = self.get(document_id, scope=scope)
        if current is None:
            raise DocumentVersionConflict("document_not_found")
        if expected_version is not None and current.version != expected_version:
            raise DocumentVersionConflict()
        tombstone = _clone(
            current,
            status=STATUS_DELETED,
            chunk_count=0,
            updated_at=utc_now(),
        )
        updated = self.update(tombstone, expected_version=current.version)
        with self._lock:
            self._chunks[document_id] = []
        return updated

    def find_by_hash(self, scope: MemoryScope, content_hash: str):
        with self._lock:
            for row in self._docs.values():
                if (
                    row.scope.key() == scope.key()
                    and row.content_hash == content_hash
                    and row.status not in {STATUS_DELETED}
                ):
                    return row
        return None

    def list_by_scope(self, scope: MemoryScope, *, statuses=None):
        allowed = set(statuses or ())
        with self._lock:
            rows = [
                r
                for r in self._docs.values()
                if r.scope.key() == scope.key()
                and (not allowed or r.status in allowed)
                and r.status != STATUS_DELETED
            ]
        return tuple(sorted(rows, key=lambda r: r.document_id))

    def save_chunks(self, document_id: str, chunks):
        if not self.available:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        with self._lock:
            self._chunks[document_id] = list(chunks)

    def list_chunks(self, document_id: str, *, scope: MemoryScope | None = None):
        with self._lock:
            if scope is not None:
                doc = self._docs.get(document_id)
                if doc is None or doc.scope.key() != scope.key():
                    return ()
            return tuple(self._chunks.get(document_id, ()))

    def get_provenance(self, document_id: str):
        with self._lock:
            return self._prov.get(document_id)

    def put_blob(self, document_id: str, data: bytes) -> None:
        with self._lock:
            self._blobs[document_id] = bytes(data)

    def get_blob(self, document_id: str) -> bytes | None:
        with self._lock:
            return self._blobs.get(document_id)

    def delete_blob(self, document_id: str) -> None:
        with self._lock:
            self._blobs.pop(document_id, None)

    def save_extract_partial(self, document_id: str, batch_index: int, payload: dict) -> None:
        with self._lock:
            bucket = self._partials.setdefault(document_id, {})
            bucket[int(batch_index)] = dict(payload)

    def list_extract_partials(self, document_id: str) -> dict[int, dict]:
        with self._lock:
            return {int(k): dict(v) for k, v in self._partials.get(document_id, {}).items()}

    def clear_extract_partials(self, document_id: str) -> None:
        with self._lock:
            self._partials.pop(document_id, None)
