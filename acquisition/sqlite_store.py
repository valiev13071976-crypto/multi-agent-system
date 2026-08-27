"""SQLite acquisition store — tenant-scoped durable persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from acquisition.errors import AcquisitionError
from acquisition.models import (
    ChangeEvent,
    FreshnessPolicy,
    ParsedRecord,
    RawArtifact,
    SourceDescriptor,
)
from acquisition.store import AcquisitionStore
from autonomy.models import sanitize_metadata
from security.config import DEFAULT_LEGACY_TENANT
from security.tenant import normalize_tenant_id, scope_tenant_ref

ACQUISITION_SCHEMA_VERSION = 1
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
    return scope_tenant_ref(normalize_tenant_id(tenant_id) or DEFAULT_LEGACY_TENANT)


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
