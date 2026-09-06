"""Persistence backends for the canonical artifact layer (Block 3.5.1, 3.5.13).

Follows the same proven pattern already used by product_media/documents/
data_intel: SQLite metadata row + SQLite blob row, WAL-friendly, tenant
column on every table, additive schema. Reuses the existing
``PANDA_ARTIFACT_ROOT`` storage location already reserved for "governed
generated artifacts and uploads" (production_foundation.storage inventory)
instead of inventing a new storage root.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from artifacts.models import ArtifactRecord, STATUS_ACTIVE


class ArtifactStoreBackend(ABC):
    """Abstract artifact metadata + blob persistence contract."""

    @abstractmethod
    def save(self, record: ArtifactRecord, blob: bytes | None) -> None: ...

    @abstractmethod
    def get(self, *, tenant_id: str, artifact_id: str) -> ArtifactRecord | None: ...

    @abstractmethod
    def get_any_tenant(self, artifact_id: str) -> ArtifactRecord | None:
        """Lookup ignoring tenant -- used only to distinguish NOT_FOUND from
        ACCESS_DENIED for telemetry/error-taxonomy purposes. Never used to
        grant access."""

    @abstractmethod
    def get_by_legacy_ref(self, *, tenant_id: str, ref: str) -> ArtifactRecord | None: ...

    @abstractmethod
    def get_blob(self, *, tenant_id: str, artifact_id: str) -> bytes | None: ...

    @abstractmethod
    def list_for_conversation(self, *, tenant_id: str, conversation_id: str) -> list[ArtifactRecord]: ...

    @abstractmethod
    def mark_status(self, *, tenant_id: str, artifact_id: str, status: str) -> bool: ...


def _row_to_record(row: dict) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        tenant_id=row["tenant_id"],
        owner_id=row["owner_id"],
        filename=row["filename"],
        safe_filename=row["safe_filename"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"]),
        kind=row["kind"],
        created_at=row["created_at"],
        storage_ref=row["storage_ref"],
        source=row["source"],
        status=row["status"],
        conversation_id=row.get("conversation_id") or "",
        message_id=row.get("message_id") or "",
        request_id=row.get("request_id") or "",
        tool_id=row.get("tool_id") or "",
        derived_from_artifact_id=row.get("derived_from_artifact_id") or "",
        content_hash=row.get("content_hash") or "",
        legacy_ref=row.get("legacy_ref") or "",
        metadata=json.loads(row.get("metadata_json") or "{}"),
    )


class InMemoryArtifactStore(ArtifactStoreBackend):
    """Deterministic, dependency-free backend for tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ArtifactRecord] = {}
        self._blobs: dict[str, bytes] = {}
        self._legacy_index: dict[tuple[str, str], str] = {}

    def save(self, record: ArtifactRecord, blob: bytes | None) -> None:
        with self._lock:
            self._records[record.artifact_id] = record
            if blob is not None:
                self._blobs[record.artifact_id] = bytes(blob)
            if record.legacy_ref:
                self._legacy_index[(record.tenant_id, record.legacy_ref)] = record.artifact_id

    def get(self, *, tenant_id: str, artifact_id: str) -> ArtifactRecord | None:
        rec = self._records.get(artifact_id)
        if rec is None or rec.tenant_id != tenant_id:
            return None
        return rec

    def get_any_tenant(self, artifact_id: str) -> ArtifactRecord | None:
        return self._records.get(artifact_id)

    def get_by_legacy_ref(self, *, tenant_id: str, ref: str) -> ArtifactRecord | None:
        aid = self._legacy_index.get((tenant_id, ref))
        if not aid:
            return None
        return self.get(tenant_id=tenant_id, artifact_id=aid)

    def get_blob(self, *, tenant_id: str, artifact_id: str) -> bytes | None:
        rec = self.get(tenant_id=tenant_id, artifact_id=artifact_id)
        if rec is None:
            return None
        return self._blobs.get(artifact_id)

    def list_for_conversation(self, *, tenant_id: str, conversation_id: str) -> list[ArtifactRecord]:
        return [
            r
            for r in self._records.values()
            if r.tenant_id == tenant_id and r.conversation_id == conversation_id
        ]

    def mark_status(self, *, tenant_id: str, artifact_id: str, status: str) -> bool:
        rec = self.get(tenant_id=tenant_id, artifact_id=artifact_id)
        if rec is None:
            return False
        with self._lock:
            self._records[artifact_id] = ArtifactRecord(
                **{**rec.__dict__, "status": status}
            )
        return True


class SqliteArtifactStore(ArtifactStoreBackend):
    """WAL SQLite metadata + blob store -- the canonical backing store for
    every artifact kind except product_media-owned images (those stay a thin
    ``product_media:{version_id}`` reference; bytes are never duplicated)."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    safe_filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    storage_ref TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    tool_id TEXT NOT NULL DEFAULT '',
                    derived_from_artifact_id TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    legacy_ref TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_tenant
                    ON artifacts(tenant_id, artifact_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_conversation
                    ON artifacts(tenant_id, conversation_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_legacy_ref
                    ON artifacts(tenant_id, legacy_ref) WHERE legacy_ref != '';

                CREATE TABLE IF NOT EXISTS artifact_blobs (
                    artifact_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    blob BLOB NOT NULL
                );
                """
            )
            self._conn.commit()

    def save(self, record: ArtifactRecord, blob: bytes | None) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO artifacts (
                    artifact_id, tenant_id, owner_id, filename, safe_filename, mime_type,
                    size_bytes, kind, created_at, storage_ref, source, status,
                    conversation_id, message_id, request_id, tool_id,
                    derived_from_artifact_id, content_hash, legacy_ref, metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.artifact_id,
                    record.tenant_id,
                    record.owner_id,
                    record.filename,
                    record.safe_filename,
                    record.mime_type,
                    int(record.size_bytes),
                    record.kind,
                    record.created_at,
                    record.storage_ref,
                    record.source,
                    record.status,
                    record.conversation_id,
                    record.message_id,
                    record.request_id,
                    record.tool_id,
                    record.derived_from_artifact_id,
                    record.content_hash,
                    record.legacy_ref,
                    json.dumps(dict(record.metadata or {})),
                ),
            )
            if blob is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO artifact_blobs(artifact_id, tenant_id, blob) VALUES (?,?,?)",
                    (record.artifact_id, record.tenant_id, sqlite3.Binary(bytes(blob))),
                )
            self._conn.commit()

    def get(self, *, tenant_id: str, artifact_id: str) -> ArtifactRecord | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id=? AND tenant_id=?",
            (artifact_id, tenant_id),
        ).fetchone()
        return _row_to_record(dict(row)) if row else None

    def get_any_tenant(self, artifact_id: str) -> ArtifactRecord | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        return _row_to_record(dict(row)) if row else None

    def get_by_legacy_ref(self, *, tenant_id: str, ref: str) -> ArtifactRecord | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE tenant_id=? AND legacy_ref=?",
            (tenant_id, ref),
        ).fetchone()
        return _row_to_record(dict(row)) if row else None

    def get_blob(self, *, tenant_id: str, artifact_id: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT blob FROM artifact_blobs WHERE artifact_id=? AND tenant_id=?",
            (artifact_id, tenant_id),
        ).fetchone()
        if row is None:
            return None
        return bytes(row["blob"])

    def list_for_conversation(self, *, tenant_id: str, conversation_id: str) -> list[ArtifactRecord]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE tenant_id=? AND conversation_id=? ORDER BY created_at",
            (tenant_id, conversation_id),
        ).fetchall()
        return [_row_to_record(dict(r)) for r in rows]

    def mark_status(self, *, tenant_id: str, artifact_id: str, status: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE artifacts SET status=? WHERE artifact_id=? AND tenant_id=?",
                (status, artifact_id, tenant_id),
            )
            self._conn.commit()
            return cur.rowcount > 0


__all__ = [
    "ArtifactStoreBackend",
    "InMemoryArtifactStore",
    "SqliteArtifactStore",
    "STATUS_ACTIVE",
]
