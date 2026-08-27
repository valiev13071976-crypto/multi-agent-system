"""SQLite memory store — dedicated connection ownership by default."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from autonomy.models import sanitize_metadata
from memory.models import (
    MEMORY_SCHEMA_VERSION,
    MemoryLink,
    MemoryProvenance,
    MemoryRecord,
    MemoryScope,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_EXPIRED,
    utc_now,
)
from memory.store import MemoryPersistenceUnavailableError, MemoryStore, MemoryVersionConflict
from security.config import DEFAULT_LEGACY_TENANT
from security.tenant import scope_tenant_ref


DDL = f"""
CREATE TABLE IF NOT EXISTS memory_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_records (
    memory_id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    workspace_id TEXT,
    project_id TEXT,
    actor_ref TEXT,
    tenant_ref TEXT,
    title TEXT,
    content_safe TEXT,
    encrypted_content TEXT,
    summary_safe TEXT,
    content_hash TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    confidence REAL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    version INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_active_dedup
ON memory_records(tenant_ref, scope_type, scope_id, memory_type, content_hash)
WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_memory_scope
ON memory_records(tenant_ref, scope_type, scope_id, status);
CREATE TABLE IF NOT EXISTS memory_provenance (
    memory_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    created_by_component TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    source_hash TEXT NOT NULL DEFAULT '',
    workflow_id TEXT,
    task_id TEXT,
    tool_id TEXT,
    external_reference TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);
CREATE TABLE IF NOT EXISTS memory_links (
    link_id TEXT PRIMARY KEY,
    from_memory_id TEXT NOT NULL,
    to_memory_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    """Backfill legacy rows and rebuild tenant-aware indexes."""
    conn.execute(
        """
        UPDATE memory_records
        SET tenant_ref=?
        WHERE tenant_ref IS NULL OR TRIM(tenant_ref)='' OR tenant_ref='_'
        """,
        (DEFAULT_LEGACY_TENANT,),
    )
    conn.execute("DROP INDEX IF EXISTS idx_memory_active_dedup")
    conn.execute("DROP INDEX IF EXISTS idx_memory_scope")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_active_dedup
        ON memory_records(tenant_ref, scope_type, scope_id, memory_type, content_hash)
        WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS idx_memory_scope
        ON memory_records(tenant_ref, scope_type, scope_id, status);
        """
    )


class SqliteMemoryStore(MemoryStore):
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
        self.fts_available = False
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
            raise ValueError("memory_store_requires_path_or_shared_connection")
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
                # Optional FTS — graceful fallback if unavailable.
                try:
                    conn.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
                        "USING fts5(memory_id UNINDEXED, content, summary, tags)"
                    )
                    self.fts_available = True
                except sqlite3.Error:
                    self.fts_available = False
                row = conn.execute(
                    "SELECT value FROM memory_schema_meta WHERE key='schema_version'"
                ).fetchone()
                current = int(row["value"]) if row is not None else 1
                if current > MEMORY_SCHEMA_VERSION:
                    raise MemoryPersistenceUnavailableError(
                        "memory_schema_version_unsupported"
                    )
                if current < 2:
                    _migrate_tenant_scope(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO memory_schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(MEMORY_SCHEMA_VERSION)),
                )
                self._commit(conn)
            except MemoryPersistenceUnavailableError:
                raise
            except Exception as exc:
                raise MemoryPersistenceUnavailableError() from exc

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

    def create(self, record: MemoryRecord, provenance: MemoryProvenance, tags: tuple[str, ...] = ()) -> MemoryRecord:
        if not self.available:
            raise MemoryPersistenceUnavailableError()
        with self._lock:
            existing = self.find_by_hash(record.scope, record.memory_type, record.content_hash)
            if existing is not None:
                return existing
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                again = self.find_by_hash(record.scope, record.memory_type, record.content_hash)
                if again is not None:
                    conn.execute("ROLLBACK")
                    return again
                all_tags = tuple(dict.fromkeys(tuple(record.tags) + tuple(tags)))
                conn.execute(
                    """
                    INSERT INTO memory_records(
                        memory_id, memory_type, scope_type, scope_id, workspace_id, project_id,
                        actor_ref, tenant_ref, title, content_safe, encrypted_content, summary_safe,
                        content_hash, source_type, source_ref, sensitivity, confidence, status,
                        created_at, updated_at, expires_at, version, metadata_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.memory_id,
                        record.memory_type,
                        record.scope.scope_type,
                        record.scope.scope_id,
                        record.scope.workspace_id,
                        record.scope.project_id,
                        record.scope.actor_ref,
                        _tenant_key(record.scope),
                        record.title,
                        record.content_safe,
                        record.encrypted_content,
                        record.summary_safe,
                        record.content_hash,
                        record.source_type,
                        record.source_ref,
                        record.sensitivity,
                        record.confidence,
                        record.status,
                        _dt_to_db(record.created_at),
                        _dt_to_db(record.updated_at),
                        _dt_to_db(record.expires_at),
                        record.version,
                        _json_dumps(dict(record.metadata_safe)),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO memory_provenance(
                        memory_id, source_type, source_id, created_by_component, ingested_at,
                        source_hash, workflow_id, task_id, tool_id, external_reference, version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.memory_id,
                        provenance.source_type,
                        provenance.source_id,
                        provenance.created_by_component,
                        _dt_to_db(provenance.ingested_at),
                        provenance.source_hash,
                        provenance.workflow_id,
                        provenance.task_id,
                        provenance.tool_id,
                        provenance.external_reference,
                        provenance.version,
                    ),
                )
                for tag in all_tags:
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_tags(memory_id, tag) VALUES (?,?)",
                        (record.memory_id, tag),
                    )
                if self.fts_available:
                    text = record.content_safe or record.summary_safe or ""
                    conn.execute(
                        "INSERT INTO memory_fts(memory_id, content, summary, tags) VALUES (?,?,?,?)",
                        (record.memory_id, text, record.summary_safe or "", " ".join(all_tags)),
                    )
                conn.commit() if self._shared is None else self._shared.maybe_autocommit()
                return self.get(record.memory_id) or record
            except sqlite3.IntegrityError:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                existing = self.find_by_hash(record.scope, record.memory_type, record.content_hash)
                if existing is not None:
                    return existing
                raise MemoryVersionConflict("duplicate_active_memory")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise MemoryPersistenceUnavailableError() from exc

    def get(self, memory_id: str, *, scope: MemoryScope | None = None) -> MemoryRecord | None:
        with self._lock:
            if scope is not None:
                row = self._connect().execute(
                    """
                    SELECT * FROM memory_records
                    WHERE memory_id=? AND tenant_ref=? AND scope_type=? AND scope_id=?
                    """,
                    (memory_id, _tenant_key(scope), scope.scope_type, scope.scope_id),
                ).fetchone()
            else:
                row = self._connect().execute(
                    "SELECT * FROM memory_records WHERE memory_id=?", (memory_id,)
                ).fetchone()
            if row is None:
                return None
            return self._row_to_record(row)

    def update(self, record: MemoryRecord, *, expected_version: int) -> MemoryRecord:
        if not self.available:
            raise MemoryPersistenceUnavailableError()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE memory_records SET
                        title=?, content_safe=?, encrypted_content=?, summary_safe=?,
                        status=?, confidence=?, expires_at=?, updated_at=?, metadata_json=?,
                        version=?
                    WHERE memory_id=? AND version=?
                    """,
                    (
                        record.title,
                        record.content_safe,
                        record.encrypted_content,
                        record.summary_safe,
                        record.status,
                        record.confidence,
                        _dt_to_db(record.expires_at),
                        _dt_to_db(record.updated_at),
                        _json_dumps(dict(record.metadata_safe)),
                        expected_version + 1,
                        record.memory_id,
                        expected_version,
                    ),
                )
                if cur.rowcount != 1:
                    raise MemoryVersionConflict()
                self._commit(conn)
            except MemoryVersionConflict:
                raise
            except sqlite3.Error as exc:
                raise MemoryPersistenceUnavailableError() from exc
            updated = self.get(record.memory_id)
            assert updated is not None
            return updated

    def delete(self, memory_id: str, *, expected_version: int | None = None, scope: MemoryScope | None = None) -> MemoryRecord:
        current = self.get(memory_id, scope=scope)
        if current is None:
            raise MemoryVersionConflict("memory_not_found")
        if expected_version is not None and current.version != expected_version:
            raise MemoryVersionConflict()
        stamp = utc_now()
        from memory.store import _clone

        tombstone = _clone(
            current,
            status=STATUS_DELETED,
            content_safe=None,
            encrypted_content=None,
            summary_safe=None,
            updated_at=stamp,
        )
        updated = self.update(tombstone, expected_version=current.version)
        with self._lock:
            conn = self._connect()
            if self.fts_available:
                try:
                    conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                    self._commit(conn)
                except sqlite3.Error:
                    pass
        return updated

    def list_by_scope(self, scope: MemoryScope, *, statuses: tuple[str, ...] | None = None) -> tuple[MemoryRecord, ...]:
        allowed = statuses or (STATUS_ACTIVE,)
        placeholders = ",".join("?" for _ in allowed)
        tenant = _tenant_key(scope)
        with self._lock:
            rows = self._connect().execute(
                f"""
                SELECT * FROM memory_records
                WHERE tenant_ref=? AND scope_type=? AND scope_id=? AND status IN ({placeholders})
                ORDER BY memory_id
                """,
                (tenant, scope.scope_type, scope.scope_id, *allowed),
            ).fetchall()
            return tuple(self._row_to_record(r) for r in rows)

    def find_by_hash(self, scope: MemoryScope, memory_type: str, content_hash: str) -> MemoryRecord | None:
        tenant = _tenant_key(scope)
        with self._lock:
            row = self._connect().execute(
                """
                SELECT * FROM memory_records
                WHERE tenant_ref=? AND scope_type=? AND scope_id=? AND memory_type=? AND content_hash=?
                  AND status='active'
                LIMIT 1
                """,
                (tenant, scope.scope_type, scope.scope_id, memory_type, content_hash),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def find_active(self, scope: MemoryScope, memory_type: str | None = None) -> tuple[MemoryRecord, ...]:
        tenant = _tenant_key(scope)
        with self._lock:
            if memory_type:
                rows = self._connect().execute(
                    """
                    SELECT * FROM memory_records
                    WHERE tenant_ref=? AND scope_type=? AND scope_id=? AND status='active' AND memory_type=?
                    ORDER BY memory_id
                    """,
                    (tenant, scope.scope_type, scope.scope_id, memory_type),
                ).fetchall()
            else:
                rows = self._connect().execute(
                    """
                    SELECT * FROM memory_records
                    WHERE tenant_ref=? AND scope_type=? AND scope_id=? AND status='active'
                    ORDER BY memory_id
                    """,
                    (tenant, scope.scope_type, scope.scope_id),
                ).fetchall()
            return tuple(self._row_to_record(r) for r in rows)

    def expire(self, memory_id: str, *, now: datetime | None = None, scope: MemoryScope | None = None) -> MemoryRecord:
        current = self.get(memory_id, scope=scope)
        if current is None:
            raise MemoryVersionConflict("memory_not_found")
        from memory.store import _clone

        updated = _clone(
            current,
            status=STATUS_EXPIRED,
            updated_at=now or utc_now(),
        )
        return self.update(updated, expected_version=current.version)

    def link(self, link: MemoryLink) -> MemoryLink:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO memory_links(link_id, from_memory_id, to_memory_id, link_type, created_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        link.link_id,
                        link.from_memory_id,
                        link.to_memory_id,
                        link.link_type,
                        _dt_to_db(link.created_at),
                    ),
                )
                self._commit(conn)
                return link
            except sqlite3.Error as exc:
                raise MemoryPersistenceUnavailableError() from exc

    def list_links(self, memory_id: str) -> tuple[MemoryLink, ...]:
        with self._lock:
            rows = self._connect().execute(
                """
                SELECT * FROM memory_links
                WHERE from_memory_id=? OR to_memory_id=?
                ORDER BY link_id
                """,
                (memory_id, memory_id),
            ).fetchall()
            return tuple(
                MemoryLink(
                    link_id=r["link_id"],
                    from_memory_id=r["from_memory_id"],
                    to_memory_id=r["to_memory_id"],
                    link_type=r["link_type"],
                    created_at=_dt_from_db(r["created_at"]),
                )
                for r in rows
            )

    def get_provenance(self, memory_id: str) -> MemoryProvenance | None:
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM memory_provenance WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row is None:
                return None
            return MemoryProvenance(
                source_type=row["source_type"],
                source_id=row["source_id"],
                created_by_component=row["created_by_component"],
                ingested_at=_dt_from_db(row["ingested_at"]),
                source_hash=row["source_hash"] or "",
                workflow_id=row["workflow_id"],
                task_id=row["task_id"],
                tool_id=row["tool_id"],
                external_reference=row["external_reference"],
                version=int(row["version"]),
            )

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        tags = tuple(
            r["tag"]
            for r in self._connect().execute(
                "SELECT tag FROM memory_tags WHERE memory_id=? ORDER BY tag",
                (row["memory_id"],),
            ).fetchall()
        )
        prov = self.get_provenance(row["memory_id"])
        if prov is None:
            prov = MemoryProvenance(
                source_type=row["source_type"],
                source_id=row["source_ref"],
                created_by_component="unknown",
                ingested_at=_dt_from_db(row["created_at"]),
            )
        return MemoryRecord(
            memory_id=row["memory_id"],
            memory_type=row["memory_type"],
            scope=MemoryScope(
                scope_type=row["scope_type"],
                scope_id=row["scope_id"],
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                actor_ref=row["actor_ref"],
                tenant_ref=row["tenant_ref"],
            ),
            content_hash=row["content_hash"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            provenance=prov,
            sensitivity=row["sensitivity"],
            status=row["status"],
            created_at=_dt_from_db(row["created_at"]),
            updated_at=_dt_from_db(row["updated_at"]),
            title=row["title"],
            content_safe=row["content_safe"],
            encrypted_content=row["encrypted_content"],
            summary_safe=row["summary_safe"],
            confidence=row["confidence"],
            tags=tags,
            expires_at=_dt_from_db(row["expires_at"]),
            version=int(row["version"]),
            metadata_safe=json.loads(row["metadata_json"] or "{}"),
        )

    def fts_search_ids(self, query: str, *, scope: MemoryScope, limit: int) -> tuple[str, ...]:
        if not self.fts_available:
            return ()
        # Escape FTS special characters by quoting tokens.
        tokens = [t for t in str(query).split() if t]
        if not tokens:
            return ()
        safe = " ".join('"' + t.replace('"', "") + '"' for t in tokens[:20])
        tenant = _tenant_key(scope)
        with self._lock:
            try:
                rows = self._connect().execute(
                    """
                    SELECT f.memory_id FROM memory_fts f
                    JOIN memory_records r ON r.memory_id = f.memory_id
                    WHERE memory_fts MATCH ?
                      AND r.tenant_ref=? AND r.scope_type=? AND r.scope_id=? AND r.status='active'
                    LIMIT ?
                    """,
                    (safe, tenant, scope.scope_type, scope.scope_id, int(limit)),
                ).fetchall()
                return tuple(r["memory_id"] for r in rows)
            except sqlite3.Error:
                return ()
