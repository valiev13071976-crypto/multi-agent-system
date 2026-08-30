"""SQLite knowledge store — tenant-partitioned durable persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from knowledge.platform_models import (
    DeletionReceipt,
    KNOWLEDGE_INDEX_VERSION,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_SUPERSEDED,
    STATUS_TOMBSTONED,
    KnowledgeChunk,
    KnowledgeIngestionJob,
    KnowledgeIndexRecord,
    KnowledgeVersion,
)
from knowledge.store import KnowledgeStore
from memory.models import MemoryScope, utc_now
from security.config import DEFAULT_LEGACY_TENANT
from security.tenant import normalize_tenant_id

DDL = """
CREATE TABLE IF NOT EXISTS knowledge_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_versions (
    version_id TEXT PRIMARY KEY,
    knowledge_id TEXT NOT NULL,
    tenant_ref TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    version_num INTEGER NOT NULL,
    status TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    index_version TEXT NOT NULL,
    supersedes_version_id TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_active_dedup
ON knowledge_versions(tenant_ref, scope_type, scope_id, source_id, content_hash)
WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_knowledge_tenant_scope
ON knowledge_versions(tenant_ref, scope_type, scope_id, status);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    knowledge_id TEXT NOT NULL,
    tenant_ref TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    content_safe TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL,
    page_ref TEXT,
    section_ref TEXT,
    char_start INTEGER,
    char_end INTEGER,
    overlap_prev INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_tenant
ON knowledge_chunks(tenant_ref, version_id, status);
CREATE TABLE IF NOT EXISTS knowledge_index_records (
    record_id TEXT PRIMARY KEY,
    chunk_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    knowledge_id TEXT NOT NULL,
    tenant_ref TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    index_version TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_index_tenant
ON knowledge_index_records(tenant_ref, embedding_model, status);
CREATE TABLE IF NOT EXISTS knowledge_ingestion_jobs (
    job_id TEXT PRIMARY KEY,
    tenant_ref TEXT NOT NULL,
    source_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    checkpoint INTEGER NOT NULL DEFAULT 0,
    chunk_total INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    profile_version TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _dt_to_db(value: datetime | None) -> str:
    if value is None:
        return utc_now().isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _scope_tenant(scope: MemoryScope) -> str:
    return normalize_tenant_id(scope.tenant_ref or DEFAULT_LEGACY_TENANT)


class SQLiteKnowledgeStore(KnowledgeStore):
    def __init__(self, path: str | Path):
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.available = True
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(DDL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save_version(self, version: KnowledgeVersion, *, scope: MemoryScope) -> KnowledgeVersion:
        tenant = _scope_tenant(scope)
        if version.tenant_ref != tenant:
            raise ValueError("tenant_mismatch")
        with self._lock:
            existing = self.find_version_by_hash(
                tenant_ref=tenant,
                scope=scope,
                source_id=version.source_id,
                content_hash=version.content_hash,
            )
            if existing is not None and existing.status == STATUS_ACTIVE:
                return existing
            self._conn.execute(
                """
                INSERT OR REPLACE INTO knowledge_versions
                (version_id, knowledge_id, tenant_ref, scope_type, scope_id, source_id,
                 content_hash, version_num, status, parser_version, chunker_version,
                 embedding_model, embedding_version, index_version, supersedes_version_id,
                 created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.version_id,
                    version.knowledge_id,
                    tenant,
                    scope.scope_type,
                    scope.scope_id,
                    version.source_id,
                    version.content_hash,
                    version.version_num,
                    version.status,
                    version.parser_version,
                    version.chunker_version,
                    version.embedding_model,
                    version.embedding_version,
                    version.index_version,
                    version.supersedes_version_id,
                    _dt_to_db(version.created_at),
                    json.dumps(dict(version.metadata_safe)),
                ),
            )
            self._conn.commit()
        return version

    def get_version(self, version_id: str, *, tenant_ref: str) -> KnowledgeVersion | None:
        tenant = normalize_tenant_id(tenant_ref)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_versions WHERE version_id = ? AND tenant_ref = ?",
                (version_id, tenant),
            ).fetchone()
        return self._row_version(row) if row else None

    def list_active_versions(
        self,
        *,
        tenant_ref: str,
        scope: MemoryScope,
        source_id: str | None = None,
    ) -> tuple[KnowledgeVersion, ...]:
        tenant = normalize_tenant_id(tenant_ref)
        sql = """
            SELECT * FROM knowledge_versions
            WHERE tenant_ref = ? AND scope_type = ? AND scope_id = ?
              AND status = ?
        """
        params: list = [tenant, scope.scope_type, scope.scope_id, STATUS_ACTIVE]
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return tuple(self._row_version(r) for r in rows)

    def find_version_by_hash(
        self,
        *,
        tenant_ref: str,
        scope: MemoryScope,
        source_id: str,
        content_hash: str,
    ) -> KnowledgeVersion | None:
        tenant = normalize_tenant_id(tenant_ref)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM knowledge_versions
                WHERE tenant_ref = ? AND scope_type = ? AND scope_id = ?
                  AND source_id = ? AND content_hash = ? AND status = ?
                """,
                (tenant, scope.scope_type, scope.scope_id, source_id, content_hash, STATUS_ACTIVE),
            ).fetchone()
        return self._row_version(row) if row else None

    def supersede_version(self, old_version_id: str, new_version_id: str, *, tenant_ref: str) -> None:
        tenant = normalize_tenant_id(tenant_ref)
        with self._lock:
            self._conn.execute(
                "UPDATE knowledge_versions SET status = ? WHERE version_id = ? AND tenant_ref = ?",
                (STATUS_SUPERSEDED, old_version_id, tenant),
            )
            self._conn.execute(
                "UPDATE knowledge_chunks SET status = ? WHERE version_id = ? AND tenant_ref = ?",
                (STATUS_SUPERSEDED, old_version_id, tenant),
            )
            self._conn.execute(
                "UPDATE knowledge_index_records SET status = ? WHERE version_id = ? AND tenant_ref = ?",
                (STATUS_SUPERSEDED, old_version_id, tenant),
            )
            self._conn.commit()

    def save_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        with self._lock:
            for ch in chunks:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO knowledge_chunks
                    (chunk_id, version_id, knowledge_id, tenant_ref, sequence, content_safe,
                     content_hash, token_estimate, scope_type, scope_id, source_id, status,
                     page_ref, section_ref, char_start, char_end, overlap_prev, created_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ch.chunk_id,
                        ch.version_id,
                        ch.knowledge_id,
                        ch.tenant_ref,
                        ch.sequence,
                        ch.content,
                        ch.content_hash,
                        ch.token_estimate,
                        ch.scope.scope_type,
                        ch.scope.scope_id,
                        ch.source_id,
                        ch.status,
                        ch.page_ref,
                        ch.section_ref,
                        ch.char_start,
                        ch.char_end,
                        ch.overlap_prev,
                        _dt_to_db(ch.created_at),
                        json.dumps(dict(ch.metadata_safe)),
                    ),
                )
            self._conn.commit()

    def list_chunks(
        self,
        *,
        tenant_ref: str,
        version_id: str | None = None,
        knowledge_id: str | None = None,
        active_only: bool = True,
    ) -> tuple[KnowledgeChunk, ...]:
        tenant = normalize_tenant_id(tenant_ref)
        sql = "SELECT * FROM knowledge_chunks WHERE tenant_ref = ?"
        params: list = [tenant]
        if version_id:
            sql += " AND version_id = ?"
            params.append(version_id)
        if knowledge_id:
            sql += " AND knowledge_id = ?"
            params.append(knowledge_id)
        if active_only:
            sql += " AND status = ?"
            params.append(STATUS_ACTIVE)
        sql += " ORDER BY sequence ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return tuple(self._row_chunk(r) for r in rows)

    def save_index_records(self, records: list[KnowledgeIndexRecord]) -> None:
        with self._lock:
            for rec in records:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO knowledge_index_records
                    (record_id, chunk_id, version_id, knowledge_id, tenant_ref,
                     embedding_model, embedding_version, embedding_dim, index_version,
                     vector_json, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec.record_id,
                        rec.chunk_id,
                        rec.version_id,
                        rec.knowledge_id,
                        rec.tenant_ref,
                        rec.embedding_model,
                        rec.embedding_version,
                        rec.embedding_dim,
                        rec.index_version,
                        json.dumps(list(rec.vector)),
                        rec.status,
                        _dt_to_db(rec.created_at),
                    ),
                )
            self._conn.commit()

    def list_index_records(
        self,
        *,
        tenant_ref: str,
        embedding_model: str,
        embedding_version: str,
        active_only: bool = True,
    ) -> tuple[KnowledgeIndexRecord, ...]:
        tenant = normalize_tenant_id(tenant_ref)
        sql = """
            SELECT * FROM knowledge_index_records
            WHERE tenant_ref = ? AND embedding_model = ? AND embedding_version = ?
        """
        params: list = [tenant, embedding_model, embedding_version]
        if active_only:
            sql += " AND status = ?"
            params.append(STATUS_ACTIVE)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return tuple(self._row_index(r) for r in rows)

    def tombstone_knowledge(
        self,
        *,
        tenant_ref: str,
        knowledge_id: str | None = None,
        source_id: str | None = None,
        version_id: str | None = None,
    ) -> DeletionReceipt:
        tenant = normalize_tenant_id(tenant_ref)
        deletion_id = str(uuid.uuid4())
        started = utc_now()
        affected_v = affected_c = affected_i = 0
        with self._lock:
            version_ids: list[str] = []
            if version_id:
                version_ids = [version_id]
            else:
                sql = "SELECT version_id FROM knowledge_versions WHERE tenant_ref = ? AND status = ?"
                params: list = [tenant, STATUS_ACTIVE]
                if knowledge_id:
                    sql += " AND knowledge_id = ?"
                    params.append(knowledge_id)
                if source_id:
                    sql += " AND source_id = ?"
                    params.append(source_id)
                version_ids = [r[0] for r in self._conn.execute(sql, params).fetchall()]

            for vid in version_ids:
                cur = self._conn.execute(
                    "UPDATE knowledge_versions SET status = ? WHERE version_id = ? AND tenant_ref = ?",
                    (STATUS_TOMBSTONED, vid, tenant),
                )
                affected_v += cur.rowcount
                cur = self._conn.execute(
                    "UPDATE knowledge_chunks SET status = ? WHERE version_id = ? AND tenant_ref = ?",
                    (STATUS_TOMBSTONED, vid, tenant),
                )
                affected_c += cur.rowcount
                cur = self._conn.execute(
                    "UPDATE knowledge_index_records SET status = ? WHERE version_id = ? AND tenant_ref = ?",
                    (STATUS_TOMBSTONED, vid, tenant),
                )
                affected_i += cur.rowcount
            self._conn.commit()

        return DeletionReceipt(
            deletion_id=deletion_id,
            tenant_ref=tenant,
            status=STATUS_TOMBSTONED,
            affected_versions=affected_v,
            affected_chunks=affected_c,
            affected_index_records=affected_i,
            started_at=started,
            completed_at=utc_now(),
        )

    def save_job(self, job: KnowledgeIngestionJob) -> KnowledgeIngestionJob:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO knowledge_ingestion_jobs
                (job_id, tenant_ref, source_id, stage, status, content_hash, checkpoint,
                 chunk_total, retry_count, profile_version, error_code, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.tenant_ref,
                    job.source_id,
                    job.stage,
                    job.status,
                    job.content_hash,
                    job.checkpoint,
                    job.chunk_total,
                    job.retry_count,
                    job.profile_version,
                    job.error_code,
                    _dt_to_db(job.created_at),
                    _dt_to_db(job.updated_at),
                ),
            )
            self._conn.commit()
        return job

    def get_job(self, job_id: str, *, tenant_ref: str) -> KnowledgeIngestionJob | None:
        tenant = normalize_tenant_id(tenant_ref)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_ingestion_jobs WHERE job_id = ? AND tenant_ref = ?",
                (job_id, tenant),
            ).fetchone()
        if not row:
            return None
        return KnowledgeIngestionJob(
            job_id=row["job_id"],
            tenant_ref=row["tenant_ref"],
            source_id=row["source_id"],
            stage=row["stage"],
            status=row["status"],
            content_hash=row["content_hash"],
            checkpoint=int(row["checkpoint"]),
            chunk_total=int(row["chunk_total"]),
            retry_count=int(row["retry_count"]),
            profile_version=row["profile_version"],
            error_code=row["error_code"],
        )

    def expire_before(self, *, tenant_ref: str, before_iso: str) -> int:
        tenant = normalize_tenant_id(tenant_ref)
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE knowledge_versions SET status = ?
                WHERE tenant_ref = ? AND status = ? AND created_at < ?
                """,
                (STATUS_DELETED, tenant, STATUS_ACTIVE, before_iso),
            )
            count = cur.rowcount
            self._conn.commit()
        return count

    def _row_version(self, row) -> KnowledgeVersion:
        return KnowledgeVersion(
            version_id=row["version_id"],
            knowledge_id=row["knowledge_id"],
            tenant_ref=row["tenant_ref"],
            source_id=row["source_id"],
            content_hash=row["content_hash"],
            version_num=int(row["version_num"]),
            status=row["status"],
            parser_version=row["parser_version"],
            chunker_version=row["chunker_version"],
            embedding_model=row["embedding_model"],
            embedding_version=row["embedding_version"],
            index_version=row["index_version"] or KNOWLEDGE_INDEX_VERSION,
            supersedes_version_id=row["supersedes_version_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_chunk(self, row) -> KnowledgeChunk:
        scope = MemoryScope(
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            tenant_ref=row["tenant_ref"],
        )
        return KnowledgeChunk(
            chunk_id=row["chunk_id"],
            version_id=row["version_id"],
            knowledge_id=row["knowledge_id"],
            tenant_ref=row["tenant_ref"],
            sequence=int(row["sequence"]),
            content=row["content_safe"],
            content_hash=row["content_hash"],
            token_estimate=int(row["token_estimate"]),
            scope=scope,
            source_id=row["source_id"],
            status=row["status"],
            page_ref=row["page_ref"],
            section_ref=row["section_ref"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            overlap_prev=int(row["overlap_prev"] or 0),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_index(self, row) -> KnowledgeIndexRecord:
        vector = tuple(float(x) for x in json.loads(row["vector_json"]))
        return KnowledgeIndexRecord(
            record_id=row["record_id"],
            chunk_id=row["chunk_id"],
            version_id=row["version_id"],
            knowledge_id=row["knowledge_id"],
            tenant_ref=row["tenant_ref"],
            embedding_model=row["embedding_model"],
            embedding_version=row["embedding_version"],
            embedding_dim=int(row["embedding_dim"]),
            index_version=row["index_version"],
            vector=vector,
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
