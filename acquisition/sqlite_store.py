"""SQLite acquisition store — tenant-scoped durable persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from acquisition.errors import AcquisitionError
from acquisition.models import (
    AcquiredResource,
    AcquisitionJob,
    ChangeEvent,
    CrawlCheckpoint,
    DatasetResult,
    FrontierEntry,
    FreshnessPolicy,
    IngestionBatchResult,
    NormalizedRecord,
    ParsedRecord,
    RawArtifact,
    SourceDescriptor,
)
from acquisition.store import AcquisitionStore
from autonomy.models import sanitize_metadata
from security.config import DEFAULT_LEGACY_TENANT
from security.tenant import MissingTenantError, normalize_tenant_id, require_tenant_id, scope_tenant_ref

ACQUISITION_SCHEMA_VERSION = 2
MAX_STORED_CONTENT_CHARS = 262_144  # 256 KiB — prefer content_ref/document_id for larger

DDL = """
CREATE TABLE IF NOT EXISTS acquisition_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS acquisition_sources (
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    freshness_json TEXT NOT NULL DEFAULT '{}',
    tool_id TEXT NOT NULL DEFAULT '',
    integration_id TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL DEFAULT '',
    allowed_domains_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_acq_sources_tenant
ON acquisition_sources(tenant_id, enabled);

CREATE TABLE IF NOT EXISTS acquisition_artifacts (
    artifact_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    content_ref TEXT NOT NULL DEFAULT '',
    content_text TEXT NOT NULL DEFAULT '',
    content_bytes_len INTEGER NOT NULL DEFAULT 0,
    document_id TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    content_trust TEXT NOT NULL DEFAULT 'untrusted_external',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_acq_artifacts_checksum
ON acquisition_artifacts(tenant_id, checksum);
CREATE INDEX IF NOT EXISTS idx_acq_artifacts_tenant_source
ON acquisition_artifacts(tenant_id, source_id, fetched_at);

CREATE TABLE IF NOT EXISTS acquisition_records (
    record_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    parser_id TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    record_type TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    natural_key TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    raw_field_refs_json TEXT NOT NULL DEFAULT '{}',
    validation_ok INTEGER NOT NULL DEFAULT 1,
    validation_errors_json TEXT NOT NULL DEFAULT '[]',
    freshness TEXT NOT NULL DEFAULT 'unknown',
    content_trust TEXT NOT NULL DEFAULT 'untrusted_external',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_acq_records_fingerprint
ON acquisition_records(tenant_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_acq_records_tenant_source
ON acquisition_records(tenant_id, source_id, record_type);
CREATE INDEX IF NOT EXISTS idx_acq_records_natural
ON acquisition_records(tenant_id, source_id, natural_key);
CREATE INDEX IF NOT EXISTS idx_acq_records_artifact
ON acquisition_records(tenant_id, artifact_id);

CREATE TABLE IF NOT EXISTS acquisition_changes (
    change_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    previous_fingerprint TEXT,
    new_fingerprint TEXT,
    changed_fields_json TEXT NOT NULL DEFAULT '[]',
    observed_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_acq_changes_tenant_source
ON acquisition_changes(tenant_id, source_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_acq_changes_record
ON acquisition_changes(tenant_id, record_id, observed_at);
"""

# Additive v2 tables — jobs, frontier, checkpoints, resources, normalized, datasets, ingest
DDL_V2 = """
CREATE TABLE IF NOT EXISTS acquisition_jobs (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    workload_class TEXT NOT NULL,
    status TEXT NOT NULL,
    workflow_id TEXT NOT NULL DEFAULT '',
    trusted_job_type TEXT NOT NULL DEFAULT '',
    execution_lane TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    parser_version TEXT NOT NULL DEFAULT '',
    normalizer_version TEXT NOT NULL DEFAULT '',
    dedupe_version TEXT NOT NULL DEFAULT '',
    ingestion_version TEXT NOT NULL DEFAULT '',
    scrape_profile_id TEXT NOT NULL DEFAULT '',
    scrape_profile_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    counters_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_acq_jobs_tenant_status
ON acquisition_jobs(tenant_id, status, created_at);

CREATE TABLE IF NOT EXISTS acquisition_frontier (
    entry_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    status TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    parent_url TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acq_frontier_job_status
ON acquisition_frontier(tenant_id, job_id, status, depth);
CREATE UNIQUE INDEX IF NOT EXISTS idx_acq_frontier_canon
ON acquisition_frontier(tenant_id, job_id, canonical_url);

CREATE TABLE IF NOT EXISTS acquisition_checkpoints (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    visited_count INTEGER NOT NULL DEFAULT 0,
    frontier_pending INTEGER NOT NULL DEFAULT 0,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    pages_failed INTEGER NOT NULL DEFAULT 0,
    pages_skipped INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT NOT NULL DEFAULT '',
    parser_version TEXT NOT NULL DEFAULT '',
    normalizer_version TEXT NOT NULL DEFAULT '',
    dedupe_version TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS acquisition_resources (
    resource_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    content_length INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    raw_artifact_ref TEXT NOT NULL DEFAULT '',
    canonical_url TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    parent_url TEXT NOT NULL DEFAULT '',
    extraction_status TEXT NOT NULL DEFAULT 'ok',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acq_resources_job
ON acquisition_resources(tenant_id, job_id, status);

CREATE TABLE IF NOT EXISTS acquisition_normalized_records (
    record_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    resource_id TEXT NOT NULL DEFAULT '',
    normalizer_version TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '{}',
    field_status_json TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acq_norm_tenant_fp
ON acquisition_normalized_records(tenant_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_acq_norm_job
ON acquisition_normalized_records(tenant_id, job_id);

CREATE TABLE IF NOT EXISTS acquisition_datasets (
    dataset_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT '',
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_acq_datasets_tenant
ON acquisition_datasets(tenant_id, job_id);

CREATE TABLE IF NOT EXISTS acquisition_ingest_progress (
    batch_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    duplicate INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_acq_ingest_idem
ON acquisition_ingest_progress(tenant_id, idempotency_key)
WHERE idempotency_key != '';
CREATE INDEX IF NOT EXISTS idx_acq_ingest_job
ON acquisition_ingest_progress(tenant_id, job_id);
"""


class AcquisitionStoreUnavailableError(AcquisitionError):
    def __init__(self, error_code: str = "acquisition_store_unavailable"):
        super().__init__(error_code)


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
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), separators=(",", ":"), sort_keys=True, default=str)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return json.dumps(value, separators=(",", ":"), default=str)
    return json.dumps(
        sanitize_metadata(value or {}),
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _json_loads(value: str | None, default=None):
    if not value:
        return default if default is not None else {}
    return json.loads(value)


def _natural_key(fields: dict) -> str:
    return str(
        fields.get("ean")
        or fields.get("sku")
        or fields.get("supplier_sku")
        or fields.get("source_sku")
        or fields.get("mpn")
        or ""
    )


def _tenant(tenant_id: str | None) -> str:
    """Legacy-compatible tenant for v1 tables (sources/artifacts/records/changes)."""
    return scope_tenant_ref(normalize_tenant_id(tenant_id) or DEFAULT_LEGACY_TENANT)


def _tenant_required(tenant_id: str | None) -> str:
    """Fail-closed tenant for v2 objects (jobs/frontier/resources/datasets)."""
    return require_tenant_id(tenant_id)


class SqliteAcquisitionStore(AcquisitionStore):
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
            raise ValueError("acquisition_store_requires_path_or_shared_connection")
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
                conn.executescript(DDL_V2)
                # Legacy empty tenant_id → default
                conn.execute(
                    """
                    UPDATE acquisition_sources
                    SET tenant_id=?
                    WHERE tenant_id IS NULL OR TRIM(tenant_id)='' OR tenant_id='_'
                    """,
                    (DEFAULT_LEGACY_TENANT,),
                )
                conn.execute(
                    """
                    UPDATE acquisition_artifacts
                    SET tenant_id=?
                    WHERE tenant_id IS NULL OR TRIM(tenant_id)='' OR tenant_id='_'
                    """,
                    (DEFAULT_LEGACY_TENANT,),
                )
                conn.execute(
                    """
                    UPDATE acquisition_records
                    SET tenant_id=?
                    WHERE tenant_id IS NULL OR TRIM(tenant_id)='' OR tenant_id='_'
                    """,
                    (DEFAULT_LEGACY_TENANT,),
                )
                conn.execute(
                    """
                    UPDATE acquisition_changes
                    SET tenant_id=?
                    WHERE tenant_id IS NULL OR TRIM(tenant_id)='' OR tenant_id='_'
                    """,
                    (DEFAULT_LEGACY_TENANT,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO acquisition_schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(ACQUISITION_SCHEMA_VERSION)),
                )
                self._commit(conn)
            except Exception as exc:
                raise AcquisitionStoreUnavailableError() from exc

    def close(self) -> None:
        with self._lock:
            if not self.owns_connection:
                return
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None

    # --- sources ---
    def save_source(self, descriptor: SourceDescriptor) -> None:
        tid = _tenant(descriptor.tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_sources(
                    tenant_id, source_id, source_type, trust_level, freshness_json,
                    tool_id, integration_id, enabled, name, allowed_domains_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tid,
                    descriptor.source_id,
                    descriptor.source_type,
                    descriptor.trust_level,
                    _json_dumps(
                        {
                            "stale_after_seconds": descriptor.freshness_policy.stale_after_seconds,
                            "unknown_if_missing_timestamp": descriptor.freshness_policy.unknown_if_missing_timestamp,
                        }
                    ),
                    descriptor.tool_id,
                    descriptor.integration_id,
                    1 if descriptor.enabled else 0,
                    descriptor.name,
                    _json_dumps(list(descriptor.allowed_domains)),
                    _json_dumps(dict(descriptor.metadata)),
                ),
            )
            self._commit(conn)

    def _row_to_source(self, row) -> SourceDescriptor:
        fresh = _json_loads(row["freshness_json"], {})
        domains = _json_loads(row["allowed_domains_json"], [])
        return SourceDescriptor(
            source_id=row["source_id"],
            source_type=row["source_type"],
            tenant_id=row["tenant_id"],
            trust_level=row["trust_level"],
            freshness_policy=FreshnessPolicy(
                stale_after_seconds=fresh.get("stale_after_seconds", 86400),
                unknown_if_missing_timestamp=bool(
                    fresh.get("unknown_if_missing_timestamp", True)
                ),
            ),
            tool_id=row["tool_id"] or "",
            integration_id=row["integration_id"] or "",
            enabled=bool(row["enabled"]),
            name=row["name"] or "",
            allowed_domains=tuple(domains or ()),
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def get_source(self, source_id: str, *, tenant_id: str) -> SourceDescriptor | None:
        tid = _tenant(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                """
                SELECT * FROM acquisition_sources
                WHERE tenant_id=? AND source_id=?
                """,
                (tid, source_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_source(row)

    def list_sources(self, *, tenant_id: str) -> tuple[SourceDescriptor, ...]:
        tid = _tenant(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM acquisition_sources WHERE tenant_id=? ORDER BY source_id",
                (tid,),
            ).fetchall()
        return tuple(self._row_to_source(r) for r in rows)

    # --- artifacts ---
    def save_artifact(self, artifact: RawArtifact) -> RawArtifact:
        tid = _tenant(artifact.tenant_id)
        with self._lock:
            conn = self._connect()
            existing = conn.execute(
                """
                SELECT artifact_id FROM acquisition_artifacts
                WHERE tenant_id=? AND checksum=?
                """,
                (tid, artifact.checksum),
            ).fetchone()
            if existing is not None:
                return self.get_artifact(existing["artifact_id"], tenant_id=tid)  # type: ignore[index]

            text = artifact.content_text or ""
            # Prefer refs for large payloads — avoid duplicating document binaries
            if artifact.document_id or artifact.content_ref:
                if len(text) > MAX_STORED_CONTENT_CHARS:
                    text = ""
            elif len(text) > MAX_STORED_CONTENT_CHARS:
                text = text[:MAX_STORED_CONTENT_CHARS]

            conn.execute(
                """
                INSERT INTO acquisition_artifacts(
                    artifact_id, tenant_id, source_id, content_type, fetched_at, checksum,
                    content_ref, content_text, content_bytes_len, document_id, url,
                    content_trust, provenance_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    tid,
                    artifact.source_id,
                    artifact.content_type,
                    _dt_to_db(artifact.fetched_at),
                    artifact.checksum,
                    artifact.content_ref or "",
                    text,
                    int(artifact.content_bytes_len or 0),
                    artifact.document_id or "",
                    artifact.url or "",
                    artifact.content_trust,
                    _json_dumps(dict(artifact.provenance)),
                    _json_dumps(dict(artifact.metadata)),
                ),
            )
            self._commit(conn)
        return artifact

    def _row_to_artifact(self, row) -> RawArtifact:
        return RawArtifact(
            artifact_id=row["artifact_id"],
            source_id=row["source_id"],
            tenant_id=row["tenant_id"],
            content_type=row["content_type"],
            fetched_at=_dt_from_db(row["fetched_at"]) or datetime.now(timezone.utc),
            checksum=row["checksum"],
            content_ref=row["content_ref"] or "",
            content_text=row["content_text"] or "",
            content_bytes_len=int(row["content_bytes_len"] or 0),
            document_id=row["document_id"] or "",
            url=row["url"] or "",
            content_trust=row["content_trust"] or "untrusted_external",
            provenance=_json_loads(row["provenance_json"], {}),
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def find_artifact_by_checksum(
        self, checksum: str, *, tenant_id: str, source_id: str | None = None
    ) -> RawArtifact | None:
        tid = _tenant(tenant_id)
        with self._lock:
            conn = self._connect()
            if source_id:
                row = conn.execute(
                    """
                    SELECT * FROM acquisition_artifacts
                    WHERE tenant_id=? AND checksum=? AND source_id=?
                    """,
                    (tid, checksum, source_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM acquisition_artifacts
                    WHERE tenant_id=? AND checksum=?
                    """,
                    (tid, checksum),
                ).fetchone()
        if row is None:
            return None
        return self._row_to_artifact(row)

    def get_artifact(self, artifact_id: str, *, tenant_id: str) -> RawArtifact | None:
        tid = _tenant(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                """
                SELECT * FROM acquisition_artifacts
                WHERE artifact_id=? AND tenant_id=?
                """,
                (artifact_id, tid),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_artifact(row)

    # --- records ---
    def save_record(self, record: ParsedRecord) -> ParsedRecord:
        tid = _tenant(record.tenant_id)
        fields = dict(record.fields)
        with self._lock:
            conn = self._connect()
            existing = conn.execute(
                """
                SELECT record_id FROM acquisition_records
                WHERE tenant_id=? AND fingerprint=?
                """,
                (tid, record.fingerprint),
            ).fetchone()
            if existing is not None:
                return self.get_record(existing["record_id"], tenant_id=tid)  # type: ignore[index]

            conn.execute(
                """
                INSERT INTO acquisition_records(
                    record_id, tenant_id, source_id, artifact_id, parser_id, parser_version,
                    record_type, fields_json, confidence, fingerprint, natural_key, observed_at,
                    provenance_json, raw_field_refs_json, validation_ok, validation_errors_json,
                    freshness, content_trust, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    tid,
                    record.source_id,
                    record.artifact_id,
                    record.parser_id,
                    record.parser_version,
                    record.record_type,
                    _json_dumps(fields),
                    float(record.confidence),
                    record.fingerprint,
                    _natural_key(fields),
                    _dt_to_db(record.observed_at),
                    _json_dumps(dict(record.provenance)),
                    _json_dumps(dict(record.raw_field_refs)),
                    1 if record.validation_ok else 0,
                    _json_dumps(list(record.validation_errors)),
                    record.freshness,
                    record.content_trust,
                    _json_dumps(dict(record.metadata)),
                ),
            )
            self._commit(conn)
        return record

    def _row_to_record(self, row) -> ParsedRecord:
        errors = _json_loads(row["validation_errors_json"], [])
        return ParsedRecord(
            record_id=row["record_id"],
            parser_id=row["parser_id"],
            parser_version=row["parser_version"],
            source_id=row["source_id"],
            artifact_id=row["artifact_id"],
            tenant_id=row["tenant_id"],
            record_type=row["record_type"],
            fields=_json_loads(row["fields_json"], {}),
            confidence=float(row["confidence"]),
            fingerprint=row["fingerprint"],
            observed_at=_dt_from_db(row["observed_at"]) or datetime.now(timezone.utc),
            provenance=_json_loads(row["provenance_json"], {}),
            raw_field_refs=_json_loads(row["raw_field_refs_json"], {}),
            validation_ok=bool(row["validation_ok"]),
            validation_errors=tuple(errors or ()),
            freshness=row["freshness"] or "unknown",
            content_trust=row["content_trust"] or "untrusted_external",
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def find_record_by_fingerprint(
        self, fingerprint: str, *, tenant_id: str, source_id: str | None = None
    ) -> ParsedRecord | None:
        tid = _tenant(tenant_id)
        with self._lock:
            conn = self._connect()
            if source_id:
                row = conn.execute(
                    """
                    SELECT * FROM acquisition_records
                    WHERE tenant_id=? AND fingerprint=? AND source_id=?
                    """,
                    (tid, fingerprint, source_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM acquisition_records
                    WHERE tenant_id=? AND fingerprint=?
                    """,
                    (tid, fingerprint),
                ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_records(
        self, *, tenant_id: str, source_id: str | None = None, record_type: str | None = None
    ) -> tuple[ParsedRecord, ...]:
        tid = _tenant(tenant_id)
        sql = "SELECT * FROM acquisition_records WHERE tenant_id=?"
        params: list = [tid]
        if source_id:
            sql += " AND source_id=?"
            params.append(source_id)
        if record_type:
            sql += " AND record_type=?"
            params.append(record_type)
        sql += " ORDER BY observed_at"
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        return tuple(self._row_to_record(r) for r in rows)

    def get_record(self, record_id: str, *, tenant_id: str) -> ParsedRecord | None:
        tid = _tenant(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                """
                SELECT * FROM acquisition_records
                WHERE record_id=? AND tenant_id=?
                """,
                (record_id, tid),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def find_previous_observation(
        self, *, tenant_id: str, source_id: str, natural_key: str
    ) -> ParsedRecord | None:
        if not natural_key:
            return None
        tid = _tenant(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                """
                SELECT * FROM acquisition_records
                WHERE tenant_id=? AND source_id=? AND natural_key=?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (tid, source_id, natural_key),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    # --- changes ---
    def save_change(self, event: ChangeEvent) -> None:
        tid = _tenant(event.tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_changes(
                    change_id, tenant_id, source_id, record_id, outcome,
                    previous_fingerprint, new_fingerprint, changed_fields_json,
                    observed_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.change_id,
                    tid,
                    event.source_id,
                    event.record_id,
                    event.outcome,
                    event.previous_fingerprint,
                    event.new_fingerprint,
                    _json_dumps(list(event.changed_fields)),
                    _dt_to_db(event.observed_at),
                    _json_dumps(dict(event.metadata)),
                ),
            )
            self._commit(conn)

    def list_changes(
        self, *, tenant_id: str, source_id: str | None = None, record_id: str | None = None
    ) -> tuple[ChangeEvent, ...]:
        tid = _tenant(tenant_id)
        sql = "SELECT * FROM acquisition_changes WHERE tenant_id=?"
        params: list = [tid]
        if source_id:
            sql += " AND source_id=?"
            params.append(source_id)
        if record_id:
            sql += " AND record_id=?"
            params.append(record_id)
        sql += " ORDER BY observed_at"
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            fields = _json_loads(row["changed_fields_json"], [])
            out.append(
                ChangeEvent(
                    change_id=row["change_id"],
                    tenant_id=row["tenant_id"],
                    source_id=row["source_id"],
                    record_id=row["record_id"],
                    outcome=row["outcome"],
                    previous_fingerprint=row["previous_fingerprint"],
                    new_fingerprint=row["new_fingerprint"],
                    changed_fields=tuple(fields or ()),
                    observed_at=_dt_from_db(row["observed_at"]) or datetime.now(timezone.utc),
                    metadata=_json_loads(row["metadata_json"], {}),
                )
            )
        return tuple(out)

    # --- v2 jobs ---
    def save_job(self, job: AcquisitionJob) -> AcquisitionJob:
        tid = _tenant_required(job.tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_jobs(
                    job_id, tenant_id, actor_id, source_id, mode, workload_class, status,
                    workflow_id, trusted_job_type, execution_lane, policy_version, parser_version,
                    normalizer_version, dedupe_version, ingestion_version, scrape_profile_id,
                    scrape_profile_version, created_at, updated_at, started_at, completed_at,
                    cancel_requested, error_code, counters_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    tid,
                    job.actor_id,
                    job.source_id,
                    job.mode,
                    job.workload_class,
                    job.status,
                    job.workflow_id,
                    job.trusted_job_type,
                    job.execution_lane,
                    job.policy_version,
                    job.parser_version,
                    job.normalizer_version,
                    job.dedupe_version,
                    job.ingestion_version,
                    job.scrape_profile_id,
                    job.scrape_profile_version,
                    _dt_to_db(job.created_at),
                    _dt_to_db(job.updated_at),
                    _dt_to_db(job.started_at),
                    _dt_to_db(job.completed_at),
                    1 if job.cancel_requested else 0,
                    job.error_code,
                    _json_dumps(dict(job.counters)),
                    _json_dumps(dict(job.metadata)),
                ),
            )
            self._commit(conn)
        return job

    def _row_to_job(self, row) -> AcquisitionJob:
        return AcquisitionJob(
            job_id=row["job_id"],
            tenant_id=row["tenant_id"],
            actor_id=row["actor_id"] or "",
            source_id=row["source_id"],
            mode=row["mode"],
            workload_class=row["workload_class"],
            status=row["status"],
            workflow_id=row["workflow_id"] or "",
            trusted_job_type=row["trusted_job_type"] or "",
            execution_lane=row["execution_lane"] or "",
            policy_version=row["policy_version"] or "",
            parser_version=row["parser_version"] or "",
            normalizer_version=row["normalizer_version"] or "",
            dedupe_version=row["dedupe_version"] or "",
            ingestion_version=row["ingestion_version"] or "",
            scrape_profile_id=row["scrape_profile_id"] or "",
            scrape_profile_version=row["scrape_profile_version"] or "",
            created_at=_dt_from_db(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt_from_db(row["updated_at"]) or datetime.now(timezone.utc),
            started_at=_dt_from_db(row["started_at"]),
            completed_at=_dt_from_db(row["completed_at"]),
            cancel_requested=bool(row["cancel_requested"]),
            error_code=row["error_code"] or "",
            counters=_json_loads(row["counters_json"], {}),
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def get_job(self, job_id: str, *, tenant_id: str) -> AcquisitionJob | None:
        tid = _tenant_required(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM acquisition_jobs WHERE job_id=? AND tenant_id=?",
                (job_id, tid),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(self, *, tenant_id: str, status: str | None = None) -> tuple[AcquisitionJob, ...]:
        tid = _tenant_required(tenant_id)
        sql = "SELECT * FROM acquisition_jobs WHERE tenant_id=?"
        params: list = [tid]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at"
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        return tuple(self._row_to_job(r) for r in rows)

    # --- frontier ---
    def save_frontier_entry(self, entry: FrontierEntry) -> FrontierEntry:
        tid = _tenant_required(entry.tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_frontier(
                    entry_id, job_id, tenant_id, url, canonical_url, status, depth,
                    parent_url, retry_count, claim_token, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.entry_id,
                    entry.job_id,
                    tid,
                    entry.url,
                    entry.canonical_url,
                    entry.status,
                    int(entry.depth),
                    entry.parent_url,
                    int(entry.retry_count),
                    entry.claim_token,
                    entry.error_code,
                    _dt_to_db(entry.created_at),
                    _dt_to_db(entry.updated_at),
                ),
            )
            self._commit(conn)
        return entry

    def list_frontier(
        self,
        *,
        job_id: str,
        tenant_id: str,
        statuses: tuple[str, ...] | None = None,
    ) -> tuple[FrontierEntry, ...]:
        tid = _tenant_required(tenant_id)
        sql = "SELECT * FROM acquisition_frontier WHERE tenant_id=? AND job_id=?"
        params: list = [tid, job_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY depth, created_at"
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            out.append(
                FrontierEntry(
                    entry_id=row["entry_id"],
                    job_id=row["job_id"],
                    tenant_id=row["tenant_id"],
                    url=row["url"],
                    canonical_url=row["canonical_url"],
                    status=row["status"],
                    depth=int(row["depth"] or 0),
                    parent_url=row["parent_url"] or "",
                    retry_count=int(row["retry_count"] or 0),
                    claim_token=row["claim_token"] or "",
                    error_code=row["error_code"] or "",
                    created_at=_dt_from_db(row["created_at"]) or datetime.now(timezone.utc),
                    updated_at=_dt_from_db(row["updated_at"]) or datetime.now(timezone.utc),
                )
            )
        return tuple(out)

    # --- checkpoints ---
    def save_checkpoint(self, checkpoint: CrawlCheckpoint) -> CrawlCheckpoint:
        tid = _tenant_required(checkpoint.tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_checkpoints(
                    job_id, tenant_id, visited_count, frontier_pending, pages_fetched,
                    pages_failed, pages_skipped, policy_version, parser_version,
                    normalizer_version, dedupe_version, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.job_id,
                    tid,
                    int(checkpoint.visited_count),
                    int(checkpoint.frontier_pending),
                    int(checkpoint.pages_fetched),
                    int(checkpoint.pages_failed),
                    int(checkpoint.pages_skipped),
                    checkpoint.policy_version,
                    checkpoint.parser_version,
                    checkpoint.normalizer_version,
                    checkpoint.dedupe_version,
                    _dt_to_db(checkpoint.updated_at),
                    _json_dumps(dict(checkpoint.metadata)),
                ),
            )
            self._commit(conn)
        return checkpoint

    def get_checkpoint(self, job_id: str, *, tenant_id: str) -> CrawlCheckpoint | None:
        tid = _tenant_required(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM acquisition_checkpoints WHERE job_id=? AND tenant_id=?",
                (job_id, tid),
            ).fetchone()
        if row is None:
            return None
        return CrawlCheckpoint(
            job_id=row["job_id"],
            tenant_id=row["tenant_id"],
            visited_count=int(row["visited_count"] or 0),
            frontier_pending=int(row["frontier_pending"] or 0),
            pages_fetched=int(row["pages_fetched"] or 0),
            pages_failed=int(row["pages_failed"] or 0),
            pages_skipped=int(row["pages_skipped"] or 0),
            policy_version=row["policy_version"] or "",
            parser_version=row["parser_version"] or "",
            normalizer_version=row["normalizer_version"] or "",
            dedupe_version=row["dedupe_version"] or "",
            updated_at=_dt_from_db(row["updated_at"]) or datetime.now(timezone.utc),
            metadata=_json_loads(row["metadata_json"], {}),
        )

    # --- resources ---
    def save_resource(self, resource: AcquiredResource) -> AcquiredResource:
        tid = _tenant_required(resource.tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_resources(
                    resource_id, job_id, tenant_id, source_id, url, status, content_type,
                    content_length, content_hash, raw_artifact_ref, canonical_url, depth,
                    parent_url, extraction_status, provenance_json, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resource.resource_id,
                    resource.job_id,
                    tid,
                    resource.source_id,
                    resource.url,
                    resource.status,
                    resource.content_type,
                    int(resource.content_length),
                    resource.content_hash,
                    resource.raw_artifact_ref,
                    resource.canonical_url,
                    int(resource.depth),
                    resource.parent_url,
                    resource.extraction_status,
                    _json_dumps(dict(resource.provenance)),
                    _json_dumps(dict(resource.metadata)),
                    _dt_to_db(resource.created_at),
                    _dt_to_db(resource.updated_at),
                ),
            )
            self._commit(conn)
        return resource

    def list_resources(
        self, *, job_id: str, tenant_id: str
    ) -> tuple[AcquiredResource, ...]:
        tid = _tenant_required(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT * FROM acquisition_resources
                WHERE tenant_id=? AND job_id=?
                ORDER BY created_at
                """,
                (tid, job_id),
            ).fetchall()
        out = []
        for row in rows:
            out.append(
                AcquiredResource(
                    resource_id=row["resource_id"],
                    job_id=row["job_id"],
                    tenant_id=row["tenant_id"],
                    source_id=row["source_id"],
                    url=row["url"],
                    status=row["status"],
                    content_type=row["content_type"] or "",
                    content_length=int(row["content_length"] or 0),
                    content_hash=row["content_hash"] or "",
                    raw_artifact_ref=row["raw_artifact_ref"] or "",
                    canonical_url=row["canonical_url"] or "",
                    depth=int(row["depth"] or 0),
                    parent_url=row["parent_url"] or "",
                    extraction_status=row["extraction_status"] or "ok",
                    provenance=_json_loads(row["provenance_json"], {}),
                    metadata=_json_loads(row["metadata_json"], {}),
                    created_at=_dt_from_db(row["created_at"]) or datetime.now(timezone.utc),
                    updated_at=_dt_from_db(row["updated_at"]) or datetime.now(timezone.utc),
                )
            )
        return tuple(out)

    def save_normalized_record(self, record: NormalizedRecord) -> NormalizedRecord:
        tid = _tenant_required(record.tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_normalized_records(
                    record_id, job_id, tenant_id, source_id, resource_id, normalizer_version,
                    fields_json, field_status_json, fingerprint, warnings_json, errors_json,
                    provenance_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.job_id,
                    tid,
                    record.source_id,
                    record.resource_id,
                    record.normalizer_version,
                    _json_dumps(dict(record.fields)),
                    _json_dumps(dict(record.field_status)),
                    record.fingerprint,
                    _json_dumps(list(record.warnings)),
                    _json_dumps(list(record.errors)),
                    _json_dumps(dict(record.provenance)),
                    _dt_to_db(record.created_at),
                ),
            )
            self._commit(conn)
        return record

    def save_dataset(self, dataset: DatasetResult) -> DatasetResult:
        tid = _tenant_required(dataset.tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_datasets(
                    dataset_id, tenant_id, job_id, name, version, record_count, fingerprint,
                    source_ids_json, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset.dataset_id,
                    tid,
                    dataset.job_id,
                    dataset.name,
                    dataset.version,
                    int(dataset.record_count),
                    dataset.fingerprint,
                    _json_dumps(list(dataset.source_ids)),
                    _dt_to_db(dataset.created_at),
                    _json_dumps(dict(dataset.metadata)),
                ),
            )
            self._commit(conn)
        return dataset

    def get_dataset(self, dataset_id: str, *, tenant_id: str) -> DatasetResult | None:
        tid = _tenant_required(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM acquisition_datasets WHERE dataset_id=? AND tenant_id=?",
                (dataset_id, tid),
            ).fetchone()
        if row is None:
            return None
        sources = _json_loads(row["source_ids_json"], [])
        return DatasetResult(
            dataset_id=row["dataset_id"],
            tenant_id=row["tenant_id"],
            job_id=row["job_id"],
            name=row["name"],
            version=row["version"],
            record_count=int(row["record_count"] or 0),
            fingerprint=row["fingerprint"] or "",
            source_ids=tuple(sources or ()),
            created_at=_dt_from_db(row["created_at"]) or datetime.now(timezone.utc),
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def save_ingest_batch(self, batch: IngestionBatchResult) -> IngestionBatchResult:
        tid = _tenant_required(batch.tenant_id)
        with self._lock:
            conn = self._connect()
            if batch.idempotency_key:
                existing = conn.execute(
                    """
                    SELECT * FROM acquisition_ingest_progress
                    WHERE tenant_id=? AND idempotency_key=?
                    """,
                    (tid, batch.idempotency_key),
                ).fetchone()
                if existing is not None:
                    return IngestionBatchResult(
                        batch_id=existing["batch_id"],
                        tenant_id=existing["tenant_id"],
                        job_id=existing["job_id"],
                        dataset_id=existing["dataset_id"],
                        accepted=int(existing["accepted"] or 0),
                        rejected=int(existing["rejected"] or 0),
                        duplicate=int(existing["duplicate"] or 0),
                        failed=int(existing["failed"] or 0),
                        reason_codes=tuple(_json_loads(existing["reason_codes_json"], []) or ()),
                        idempotency_key=existing["idempotency_key"] or "",
                        created_at=_dt_from_db(existing["created_at"]) or datetime.now(timezone.utc),
                        metadata=_json_loads(existing["metadata_json"], {}),
                    )
            conn.execute(
                """
                INSERT OR REPLACE INTO acquisition_ingest_progress(
                    batch_id, tenant_id, job_id, dataset_id, accepted, rejected, duplicate,
                    failed, reason_codes_json, idempotency_key, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.batch_id,
                    tid,
                    batch.job_id,
                    batch.dataset_id,
                    int(batch.accepted),
                    int(batch.rejected),
                    int(batch.duplicate),
                    int(batch.failed),
                    _json_dumps(list(batch.reason_codes)),
                    batch.idempotency_key,
                    _dt_to_db(batch.created_at),
                    _json_dumps(dict(batch.metadata)),
                ),
            )
            self._commit(conn)
        return batch
