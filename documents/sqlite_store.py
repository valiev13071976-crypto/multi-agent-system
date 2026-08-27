"""SQLite document store — dedicated or shared connection."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from autonomy.models import sanitize_metadata
from documents.errors import DOCUMENT_STORE_UNAVAILABLE, DocumentError
from documents.models import (
    DOCUMENT_SCHEMA_VERSION,
    DocumentChunkRecord,
    DocumentProvenance,
    DocumentRecord,
    STATUS_DELETED,
)
from documents.store import DocumentStore, DocumentVersionConflict, _clone
from memory.models import MemoryScope, utc_now
from security.config import DEFAULT_LEGACY_TENANT
from security.tenant import scope_tenant_ref


DDL = f"""
CREATE TABLE IF NOT EXISTS document_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    workspace_id TEXT,
    project_id TEXT,
    actor_ref TEXT,
    tenant_ref TEXT,
    filename_safe TEXT NOT NULL,
    media_type TEXT NOT NULL,
    document_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    page_count INTEGER,
    sheet_count INTEGER,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT,
    title TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_active_dedup
ON documents(tenant_ref, scope_type, scope_id, content_hash)
WHERE status NOT IN ('deleted');
CREATE INDEX IF NOT EXISTS idx_documents_scope
ON documents(tenant_ref, scope_type, scope_id, status);
CREATE TABLE IF NOT EXISTS document_provenance (
    document_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    ingested_by TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    source_hash TEXT NOT NULL DEFAULT '',
    workflow_id TEXT,
    task_id TEXT,
    parser_version TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS document_tags (
    document_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (document_id, tag)
);
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    source_location TEXT NOT NULL,
    content_safe TEXT,
    encrypted_content TEXT,
    sensitivity TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{{}}',
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_chunks_doc
ON document_chunks(document_id, ordinal);
CREATE TABLE IF NOT EXISTS document_blobs (
    document_id TEXT PRIMARY KEY,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_extract_partials (
    document_id TEXT NOT NULL,
    batch_index INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (document_id, batch_index)
);
"""


def _dt_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_db(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _json_dumps(value) -> str:
    return json.dumps(sanitize_metadata(value or {}), separators=(",", ":"), sort_keys=True)


def _tenant_key(scope: MemoryScope) -> str:
    return scope_tenant_ref(scope.tenant_ref)


def _migrate_tenant_scope(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE documents
        SET tenant_ref=?
        WHERE tenant_ref IS NULL OR TRIM(tenant_ref)='' OR tenant_ref='_'
        """,
        (DEFAULT_LEGACY_TENANT,),
    )
    conn.execute("DROP INDEX IF EXISTS idx_documents_active_dedup")
    conn.execute("DROP INDEX IF EXISTS idx_documents_scope")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_active_dedup
        ON documents(tenant_ref, scope_type, scope_id, content_hash)
        WHERE status NOT IN ('deleted');
        CREATE INDEX IF NOT EXISTS idx_documents_scope
        ON documents(tenant_ref, scope_type, scope_id, status);
        """
    )


class SqliteDocumentStore(DocumentStore):
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        shared_connection=None,
        owns_connection: bool | None = None,
    ):
        self._lock = threading.RLock()
        self._local = threading.local()
        self.available = True
        self._shared = shared_connection
        if shared_connection is not None:
            self.path = Path(getattr(shared_connection, "path", ".") or ".")
            self.owns_connection = False if owns_connection is None else bool(owns_connection)
            self.connection_mode = "shared"
            self.persistence_backend = "sqlite"
        elif db_path is not None and str(db_path).strip():
            self.path = Path(str(db_path).strip())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.owns_connection = True if owns_connection is None else bool(owns_connection)
            self.connection_mode = "dedicated"
            self.persistence_backend = "sqlite"
        else:
            raise ValueError("document_store_requires_path_or_shared_connection")
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared.connect()
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _commit(self, conn: sqlite3.Connection) -> None:
        if self._shared is not None:
            self._shared.maybe_autocommit()
            return
        conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(DDL)
                row = conn.execute(
                    "SELECT value FROM document_schema_meta WHERE key='schema_version'"
                ).fetchone()
                current = int(row["value"] if not isinstance(row, tuple) else row[0]) if row is not None else 1
                if current > DOCUMENT_SCHEMA_VERSION:
                    raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
                if current < 2:
                    _migrate_tenant_scope(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO document_schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(DOCUMENT_SCHEMA_VERSION)),
                )
                self._commit(conn)
            except DocumentError:
                raise
            except Exception as exc:
                raise DocumentError(DOCUMENT_STORE_UNAVAILABLE) from exc

    def close(self) -> None:
        with self._lock:
            if not self.owns_connection:
                return
            if self._shared is not None:
                try:
                    self._shared.close()
                except Exception:
                    pass
                return
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None

    def create(self, record, provenance, tags=()):
        if not self.available:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        with self._lock:
            existing = self.find_by_hash(record.scope, record.content_hash)
            if existing is not None:
                return existing
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                again = self.find_by_hash(record.scope, record.content_hash)
                if again is not None:
                    conn.execute("ROLLBACK")
                    return again
                conn.execute(
                    """
                    INSERT INTO documents(
                        document_id, scope_type, scope_id, workspace_id, project_id, actor_ref, tenant_ref,
                        filename_safe, media_type, document_type, size_bytes, content_hash, source_type,
                        source_ref, sensitivity, status, created_at, updated_at, version, metadata_json,
                        page_count, sheet_count, chunk_count, parser_version, title, warnings_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.document_id,
                        record.scope.scope_type,
                        record.scope.scope_id,
                        record.scope.workspace_id,
                        record.scope.project_id,
                        record.scope.actor_ref,
                        _tenant_key(record.scope),
                        record.filename_safe,
                        record.media_type,
                        record.document_type,
                        record.size_bytes,
                        record.content_hash,
                        record.source_type,
                        record.source_ref,
                        record.sensitivity,
                        record.status,
                        _dt_to_db(record.created_at),
                        _dt_to_db(record.updated_at),
                        record.version,
                        _json_dumps(dict(record.metadata_safe)),
                        record.page_count,
                        record.sheet_count,
                        record.chunk_count,
                        record.parser_version,
                        record.title,
                        json.dumps(list(record.warnings)),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO document_provenance(
                        document_id, source_type, source_id, ingested_by, ingested_at,
                        source_hash, workflow_id, task_id, parser_version
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.document_id,
                        provenance.source_type,
                        provenance.source_id,
                        provenance.ingested_by,
                        _dt_to_db(provenance.ingested_at),
                        provenance.source_hash,
                        provenance.workflow_id,
                        provenance.task_id,
                        provenance.parser_version,
                    ),
                )
                for tag in tags:
                    conn.execute(
                        "INSERT OR IGNORE INTO document_tags(document_id, tag) VALUES (?,?)",
                        (record.document_id, tag),
                    )
                self._commit(conn)
                return self.get(record.document_id) or record
            except sqlite3.IntegrityError:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                existing = self.find_by_hash(record.scope, record.content_hash)
                if existing is not None:
                    return existing
                raise DocumentVersionConflict("duplicate_document")
            except DocumentError:
                raise
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise DocumentError(DOCUMENT_STORE_UNAVAILABLE) from exc

    def get(self, document_id: str, *, scope: MemoryScope | None = None):
        with self._lock:
            if scope is not None:
                row = self._connect().execute(
                    """
                    SELECT * FROM documents
                    WHERE document_id=? AND tenant_ref=? AND scope_type=? AND scope_id=?
                    """,
                    (document_id, _tenant_key(scope), scope.scope_type, scope.scope_id),
                ).fetchone()
            else:
                row = self._connect().execute(
                    "SELECT * FROM documents WHERE document_id=?", (document_id,)
                ).fetchone()
            return self._row_to_record(row) if row else None

    def update(self, record, *, expected_version: int):
        if not self.available:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                UPDATE documents SET
                    status=?, metadata_json=?, page_count=?, sheet_count=?, chunk_count=?,
                    parser_version=?, title=?, warnings_json=?, updated_at=?, version=?
                WHERE document_id=? AND version=?
                """,
                (
                    record.status,
                    _json_dumps(dict(record.metadata_safe)),
                    record.page_count,
                    record.sheet_count,
                    record.chunk_count,
                    record.parser_version,
                    record.title,
                    json.dumps(list(record.warnings)),
                    _dt_to_db(record.updated_at or utc_now()),
                    expected_version + 1,
                    record.document_id,
                    expected_version,
                ),
            )
            if cur.rowcount != 1:
                raise DocumentVersionConflict()
            self._commit(conn)
            updated = self.get(record.document_id)
            assert updated is not None
            return updated

    def delete(self, document_id: str, *, expected_version: int | None = None, scope: MemoryScope | None = None):
        current = self.get(document_id, scope=scope)
        if current is None:
            raise DocumentVersionConflict("document_not_found")
        if expected_version is not None and current.version != expected_version:
            raise DocumentVersionConflict()
        tombstone = _clone(current, status=STATUS_DELETED, chunk_count=0, updated_at=utc_now())
        updated = self.update(tombstone, expected_version=current.version)
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
            self._commit(conn)
        return updated

    def find_by_hash(self, scope: MemoryScope, content_hash: str):
        tenant = _tenant_key(scope)
        with self._lock:
            row = self._connect().execute(
                """
                SELECT * FROM documents
                WHERE tenant_ref=? AND scope_type=? AND scope_id=? AND content_hash=?
                  AND status != 'deleted'
                LIMIT 1
                """,
                (tenant, scope.scope_type, scope.scope_id, content_hash),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def list_by_scope(self, scope: MemoryScope, *, statuses=None):
        tenant = _tenant_key(scope)
        with self._lock:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = self._connect().execute(
                    f"""
                    SELECT * FROM documents
                    WHERE tenant_ref=? AND scope_type=? AND scope_id=? AND status IN ({placeholders})
                    ORDER BY document_id
                    """,
                    (tenant, scope.scope_type, scope.scope_id, *statuses),
                ).fetchall()
            else:
                rows = self._connect().execute(
                    """
                    SELECT * FROM documents
                    WHERE tenant_ref=? AND scope_type=? AND scope_id=? AND status != 'deleted'
                    ORDER BY document_id
                    """,
                    (tenant, scope.scope_type, scope.scope_id),
                ).fetchall()
            return tuple(self._row_to_record(r) for r in rows)

    def save_chunks(self, document_id: str, chunks):
        if not self.available:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
                for ch in chunks:
                    conn.execute(
                        """
                        INSERT INTO document_chunks(
                            chunk_id, document_id, scope_type, scope_id, ordinal, content_hash,
                            source_location, content_safe, encrypted_content, sensitivity,
                            provenance_json, metadata_json, created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            ch.chunk_id,
                            ch.document_id,
                            ch.scope.scope_type,
                            ch.scope.scope_id,
                            ch.ordinal,
                            ch.content_hash,
                            ch.source_location,
                            ch.content_safe,
                            ch.encrypted_content,
                            ch.sensitivity,
                            _json_dumps(dict(ch.provenance_json)),
                            _json_dumps(dict(ch.metadata_safe)),
                            _dt_to_db(ch.created_at),
                        ),
                    )
                self._commit(conn)
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise DocumentError(DOCUMENT_STORE_UNAVAILABLE) from exc

    def list_chunks(self, document_id: str, *, scope: MemoryScope | None = None):
        with self._lock:
            if scope is not None:
                rows = self._connect().execute(
                    """
                    SELECT c.* FROM document_chunks c
                    JOIN documents d ON d.document_id = c.document_id
                    WHERE c.document_id=? AND d.tenant_ref=? AND d.scope_type=? AND d.scope_id=?
                    ORDER BY c.ordinal
                    """,
                    (document_id, _tenant_key(scope), scope.scope_type, scope.scope_id),
                ).fetchall()
            else:
                rows = self._connect().execute(
                    """
                    SELECT * FROM document_chunks WHERE document_id=? ORDER BY ordinal
                    """,
                    (document_id,),
                ).fetchall()
            return tuple(self._chunk_from_row(r) for r in rows)

    def get_provenance(self, document_id: str):
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM document_provenance WHERE document_id=?",
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            return DocumentProvenance(
                source_type=row["source_type"],
                source_id=row["source_id"],
                ingested_by=row["ingested_by"],
                ingested_at=_dt_from_db(row["ingested_at"]),
                source_hash=row["source_hash"] or "",
                workflow_id=row["workflow_id"],
                task_id=row["task_id"],
                parser_version=row["parser_version"] or "",
            )

    def put_blob(self, document_id: str, data: bytes) -> None:
        if not self.available:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO document_blobs(document_id, content, created_at)
                VALUES (?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET content=excluded.content, created_at=excluded.created_at
                """,
                (document_id, bytes(data), _dt_to_db(utc_now())),
            )
            self._commit(conn)

    def get_blob(self, document_id: str) -> bytes | None:
        with self._lock:
            row = self._connect().execute(
                "SELECT content FROM document_blobs WHERE document_id=?",
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            return bytes(row["content"])

    def delete_blob(self, document_id: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM document_blobs WHERE document_id=?", (document_id,))
            self._commit(conn)

    def save_extract_partial(self, document_id: str, batch_index: int, payload: dict) -> None:
        if not self.available:
            raise DocumentError(DOCUMENT_STORE_UNAVAILABLE)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO document_extract_partials(document_id, batch_index, payload_json, created_at)
                VALUES (?,?,?,?)
                ON CONFLICT(document_id, batch_index) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (
                    document_id,
                    int(batch_index),
                    _json_dumps(dict(payload)),
                    _dt_to_db(utc_now()),
                ),
            )
            self._commit(conn)

    def list_extract_partials(self, document_id: str) -> dict[int, dict]:
        with self._lock:
            rows = self._connect().execute(
                """
                SELECT batch_index, payload_json FROM document_extract_partials
                WHERE document_id=? ORDER BY batch_index
                """,
                (document_id,),
            ).fetchall()
            out = {}
            for row in rows:
                try:
                    out[int(row["batch_index"])] = json.loads(row["payload_json"] or "{}")
                except Exception:
                    out[int(row["batch_index"])] = {}
            return out

    def clear_extract_partials(self, document_id: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "DELETE FROM document_extract_partials WHERE document_id=?",
                (document_id,),
            )
            self._commit(conn)

    def _row_to_record(self, row) -> DocumentRecord:
        scope = MemoryScope(
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            actor_ref=row["actor_ref"],
            tenant_ref=row["tenant_ref"],
        )
        prov = self.get_provenance(row["document_id"])
        if prov is None:
            prov = DocumentProvenance(
                source_type=row["source_type"],
                source_id=row["source_ref"],
                ingested_by="unknown",
                ingested_at=_dt_from_db(row["created_at"]),
                source_hash=row["content_hash"],
            )
        warnings = tuple(json.loads(row["warnings_json"] or "[]"))
        return DocumentRecord(
            document_id=row["document_id"],
            scope=scope,
            filename_safe=row["filename_safe"],
            media_type=row["media_type"],
            document_type=row["document_type"],
            size_bytes=int(row["size_bytes"]),
            content_hash=row["content_hash"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            provenance=prov,
            sensitivity=row["sensitivity"],
            status=row["status"],
            created_at=_dt_from_db(row["created_at"]),
            updated_at=_dt_from_db(row["updated_at"]),
            version=int(row["version"]),
            metadata_safe=json.loads(row["metadata_json"] or "{}"),
            page_count=row["page_count"],
            sheet_count=row["sheet_count"],
            chunk_count=int(row["chunk_count"] or 0),
            parser_version=row["parser_version"],
            title=row["title"],
            warnings=warnings,
        )

    def _chunk_from_row(self, row) -> DocumentChunkRecord:
        return DocumentChunkRecord(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            scope=MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"]),
            ordinal=int(row["ordinal"]),
            content_hash=row["content_hash"],
            source_location=row["source_location"],
            content_safe=row["content_safe"],
            encrypted_content=row["encrypted_content"],
            sensitivity=row["sensitivity"],
            provenance_json=json.loads(row["provenance_json"] or "{}"),
            metadata_safe=json.loads(row["metadata_json"] or "{}"),
            created_at=_dt_from_db(row["created_at"]),
        )
