"""SQLite durable backends for side-effect execution, idempotency, and reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autonomy.models import IdempotencyRecord, sanitize_metadata
from security.encryption import (
    ENCRYPTION_REQUIRED,
    SENSITIVITY_INTERNAL,
    EncryptedPayload,
    EncryptionService,
    EncryptionUnavailableError,
)
from side_effects.errors import (
    SideEffectPersistenceConflictError,
    SideEffectPersistenceUnavailableError,
)
from side_effects.models import ReconciliationRecord, SideEffectExecutionRecord
from side_effects.schema import (
    DDL,
    MAX_ENCRYPTED_PAYLOAD_BYTES,
    MAX_SAFE_METADATA_BYTES,
    SCHEMA_VERSION,
)


def hash_idempotency_storage_key(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _dt_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_db(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _json_dumps(value: Any) -> str:
    try:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SideEffectPersistenceUnavailableError(
            "side_effect_metadata_invalid"
        ) from exc
    if len(raw.encode("utf-8")) > MAX_SAFE_METADATA_BYTES:
        raise SideEffectPersistenceUnavailableError("side_effect_metadata_too_large")
    return raw


def _json_loads(raw: str | None) -> dict:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    return data


def _split_payload(
    metadata: dict | None,
    *,
    sensitivity: str,
    encryption: EncryptionService | None,
    sensitive_keys: frozenset[str] | None = None,
) -> tuple[str, str | None]:
    meta = sanitize_metadata(metadata or {})
    sensitive_keys = sensitive_keys or frozenset()
    safe: dict[str, Any] = {}
    sensitive: dict[str, Any] = {}
    for key, value in meta.items():
        if str(key) in sensitive_keys or sensitivity in ENCRYPTION_REQUIRED:
            sensitive[str(key)] = value
        else:
            safe[str(key)] = value
    # GitHub rollback refs stay in safe column when structured as prior_present/changed only.
    safe_json = _json_dumps(safe)
    encrypted_json = None
    if sensitive or sensitivity in ENCRYPTION_REQUIRED:
        if encryption is None:
            raise EncryptionUnavailableError(
                "Sensitive side-effect persistence requires EncryptionService."
            )
        envelope = encryption.encrypt(_json_dumps(sensitive or meta))
        encrypted_json = envelope.serialize()
        if len(encrypted_json.encode("utf-8")) > MAX_ENCRYPTED_PAYLOAD_BYTES:
            raise SideEffectPersistenceUnavailableError(
                "side_effect_encrypted_payload_too_large"
            )
    return safe_json, encrypted_json


def _merge_payload(
    safe_json: str | None,
    encrypted_json: str | None,
    *,
    encryption: EncryptionService | None,
) -> dict:
    merged = dict(_json_loads(safe_json))
    if encrypted_json:
        if encryption is None:
            raise EncryptionUnavailableError(
                "Encrypted side-effect payload requires EncryptionService."
            )
        plain = encryption.decrypt(EncryptedPayload.deserialize(encrypted_json))
        extra = _json_loads(plain)
        merged.update(extra)
    return sanitize_metadata(merged)


class SqliteConnection:
    """Thread-local SQLite connection manager for side-effect persistence."""

    def __init__(self, path: str | Path):
        self.path = str(Path(path))
        self._local = threading.local()
        self._lock = threading.RLock()
        self._uow_depth = 0

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        parent = Path(self.path).parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False)
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "side_effect_persistence_unavailable"
            ) from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        self._local.conn = conn
        self._maybe_restrict_permissions()
        return conn

    def _maybe_restrict_permissions(self) -> None:
        try:
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except OSError:
            return

    def initialize_schema(self) -> int:
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(DDL)
                row = conn.execute(
                    "SELECT version FROM side_effect_schema_meta WHERE id = 1"
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO side_effect_schema_meta(id, version) VALUES (1, ?)",
                        (SCHEMA_VERSION,),
                    )
                    conn.commit()
                    return SCHEMA_VERSION
                version = int(row["version"])
                if version != SCHEMA_VERSION:
                    raise SideEffectPersistenceUnavailableError(
                        "side_effect_schema_version_unsupported"
                    )
                conn.commit()
                return version
            except SideEffectPersistenceUnavailableError:
                raise
            except sqlite3.Error as exc:
                raise SideEffectPersistenceUnavailableError(
                    "side_effect_persistence_unavailable"
                ) from exc

    def get_schema_version(self) -> int:
        conn = self.connect()
        row = conn.execute(
            "SELECT version FROM side_effect_schema_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            raise SideEffectPersistenceUnavailableError(
                "side_effect_schema_uninitialized"
            )
        version = int(row["version"])
        if version != SCHEMA_VERSION:
            raise SideEffectPersistenceUnavailableError(
                "side_effect_schema_version_unsupported"
            )
        return version

    def begin(self) -> sqlite3.Connection:
        conn = self.connect()
        if self._uow_depth == 0:
            conn.execute("BEGIN IMMEDIATE")
        self._uow_depth += 1
        return conn

    def commit(self) -> None:
        if self._uow_depth > 0:
            self._uow_depth -= 1
            if self._uow_depth == 0:
                self.connect().commit()
            return
        self.connect().commit()

    def rollback(self) -> None:
        try:
            self._uow_depth = 0
            self.connect().rollback()
        except sqlite3.Error:
            return

    def maybe_autocommit(self) -> None:
        if self._uow_depth == 0:
            self.connect().commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                if self._uow_depth == 0:
                    conn.commit()
            except sqlite3.Error:
                pass
            conn.close()
            self._local.conn = None
            self._uow_depth = 0


class SideEffectPersistenceUnitOfWork:
    """BEGIN → mutate stores → COMMIT/ROLLBACK for local durability only."""

    def __init__(self, connection: SqliteConnection):
        self._connection = connection

    def __enter__(self):
        self._connection.begin()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._connection.rollback()
            return False
        try:
            self._connection.commit()
        except sqlite3.Error as err:
            self._connection.rollback()
            raise SideEffectPersistenceUnavailableError(
                "side_effect_persistence_unavailable"
            ) from err
        return False


class PersistentSideEffectExecutionStore:
    def __init__(
        self,
        connection: SqliteConnection,
        *,
        encryption: EncryptionService | None = None,
    ):
        self._connection = connection
        self._encryption = encryption

    def create(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord:
        return self._upsert(record, insert=True)

    def get(self, execution_id: str) -> SideEffectExecutionRecord | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM side_effect_executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def save(self, record: SideEffectExecutionRecord) -> SideEffectExecutionRecord:
        return self._upsert(record, insert=False)

    def find_by_action(self, action_id: str) -> tuple[SideEffectExecutionRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM side_effect_executions WHERE action_id = ?",
            (action_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_by_idempotency(
        self, idempotency_key_hash: str
    ) -> SideEffectExecutionRecord | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM side_effect_executions WHERE idempotency_key_hash = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (idempotency_key_hash,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def list_by_workflow(
        self, workflow_id: str
    ) -> tuple[SideEffectExecutionRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM side_effect_executions WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_all(self) -> tuple[SideEffectExecutionRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute("SELECT * FROM side_effect_executions").fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_by_status(self, status: str) -> tuple[SideEffectExecutionRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM side_effect_executions WHERE status = ?",
            (status,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def _upsert(
        self, record: SideEffectExecutionRecord, *, insert: bool
    ) -> SideEffectExecutionRecord:
        safe_json, encrypted_json = _split_payload(
            dict(record.metadata),
            sensitivity=SENSITIVITY_INTERNAL,
            encryption=self._encryption,
            sensitive_keys=frozenset(),
        )
        conn = self._connection.connect()
        values = {
            "execution_id": record.execution_id,
            "workflow_id": record.workflow_id,
            "task_id": record.task_id,
            "action_id": record.action_id,
            "tool_id": record.tool_id,
            "operation": record.operation,
            "resource_ref": record.resource_ref,
            "status": record.status,
            "outcome": record.outcome,
            "authorization_type": record.authorization_type,
            "authorization_id": record.authorization_id,
            "idempotency_key_hash": record.idempotency_key_hash,
            "attempt": int(record.attempt),
            "started_at": _dt_to_db(record.started_at),
            "completed_at": _dt_to_db(record.completed_at),
            "external_reference": record.external_reference,
            "reversible": 1 if record.reversible else 0,
            "rollback_reference": record.rollback_reference,
            "rollback_status": record.rollback_status,
            "error_code": record.error_code,
            "parent_execution_id": record.parent_execution_id,
            "reconciliation_id": record.reconciliation_id,
            "recovery_attempt": int(record.recovery_attempt),
            "version": int(record.version),
            "sensitivity": SENSITIVITY_INTERNAL,
            "safe_metadata_json": safe_json,
            "encrypted_payload_json": encrypted_json,
        }
        try:
            if insert:
                cols = ", ".join(values.keys())
                placeholders = ", ".join("?" for _ in values)
                conn.execute(
                    f"INSERT INTO side_effect_executions ({cols}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
            else:
                set_cols = [
                    "workflow_id",
                    "task_id",
                    "action_id",
                    "tool_id",
                    "operation",
                    "resource_ref",
                    "status",
                    "outcome",
                    "authorization_type",
                    "authorization_id",
                    "idempotency_key_hash",
                    "attempt",
                    "started_at",
                    "completed_at",
                    "external_reference",
                    "reversible",
                    "rollback_reference",
                    "rollback_status",
                    "error_code",
                    "parent_execution_id",
                    "reconciliation_id",
                    "recovery_attempt",
                    "version",
                    "sensitivity",
                    "safe_metadata_json",
                    "encrypted_payload_json",
                ]
                expected = max(int(record.version) - 1, 0)
                assignment = ", ".join(f"{col}=?" for col in set_cols)
                params = [values[col] for col in set_cols]
                params.extend([record.execution_id, expected])
                cur = conn.execute(
                    f"UPDATE side_effect_executions SET {assignment} "
                    "WHERE execution_id=? AND version=?",
                    params,
                )
                if cur.rowcount != 1:
                    raise SideEffectPersistenceConflictError()
            self._connection.maybe_autocommit()
        except SideEffectPersistenceConflictError:
            raise
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "side_effect_persistence_unavailable"
            ) from exc
        return record

    def _from_row(self, row: sqlite3.Row) -> SideEffectExecutionRecord:
        metadata = _merge_payload(
            row["safe_metadata_json"],
            row["encrypted_payload_json"],
            encryption=self._encryption,
        )
        return SideEffectExecutionRecord(
            execution_id=row["execution_id"],
            action_id=row["action_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            tool_id=row["tool_id"],
            operation=row["operation"],
            status=row["status"],
            authorization_type=row["authorization_type"],
            authorization_id=row["authorization_id"],
            idempotency_key_hash=row["idempotency_key_hash"],
            attempt=int(row["attempt"]),
            started_at=_dt_from_db(row["started_at"]),
            completed_at=_dt_from_db(row["completed_at"]),
            error_code=row["error_code"],
            external_reference=row["external_reference"],
            rollback_status=row["rollback_status"],
            rollback_reference=row["rollback_reference"],
            outcome=row["outcome"],
            parent_execution_id=row["parent_execution_id"],
            reconciliation_id=row["reconciliation_id"],
            recovery_attempt=int(row["recovery_attempt"] or 0),
            resource_ref=row["resource_ref"],
            reversible=bool(row["reversible"]),
            version=int(row["version"]),
            metadata=metadata,
        )


class PersistentIdempotencyStore:
    """Stores key_hash only; API still accepts raw keys via IdempotencyRegistry."""

    def __init__(
        self,
        connection: SqliteConnection,
        *,
        encryption: EncryptionService | None = None,
    ):
        self._connection = connection
        self._encryption = encryption
        self._key_cache: dict[str, str] = {}

    def put(self, record: IdempotencyRecord) -> None:
        key_hash = hash_idempotency_storage_key(record.key)
        self._key_cache[key_hash] = record.key
        safe_json, encrypted_json = _split_payload(
            dict(record.metadata),
            sensitivity=SENSITIVITY_INTERNAL,
            encryption=self._encryption,
        )
        conn = self._connection.connect()
        existing = conn.execute(
            "SELECT version FROM idempotency_records WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        try:
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO idempotency_records (
                        key_hash, action_id, state, created_at, updated_at,
                        execution_id, version, sensitivity, safe_metadata_json,
                        encrypted_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key_hash,
                        record.action_id,
                        record.state,
                        _dt_to_db(record.created_at),
                        _dt_to_db(record.updated_at),
                        record.execution_id,
                        int(record.version),
                        SENSITIVITY_INTERNAL,
                        safe_json,
                        encrypted_json,
                    ),
                )
            else:
                expected = int(record.version) - 1
                cur = conn.execute(
                    """
                    UPDATE idempotency_records SET
                        action_id=?, state=?, updated_at=?, execution_id=?,
                        version=?, safe_metadata_json=?, encrypted_payload_json=?
                    WHERE key_hash=? AND version=?
                    """,
                    (
                        record.action_id,
                        record.state,
                        _dt_to_db(record.updated_at),
                        record.execution_id,
                        int(record.version),
                        safe_json,
                        encrypted_json,
                        key_hash,
                        expected,
                    ),
                )
                if cur.rowcount != 1:
                    raise SideEffectPersistenceConflictError()
            self._connection.maybe_autocommit()
        except SideEffectPersistenceConflictError:
            raise
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "side_effect_persistence_unavailable"
            ) from exc

    def get(self, key: str) -> IdempotencyRecord | None:
        key_hash = hash_idempotency_storage_key(key)
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        if row is None:
            return None
        self._key_cache[key_hash] = key
        metadata = _merge_payload(
            row["safe_metadata_json"],
            row["encrypted_payload_json"],
            encryption=self._encryption,
        )
        return IdempotencyRecord(
            key=key,
            action_id=row["action_id"],
            state=row["state"],
            created_at=_dt_from_db(row["created_at"]),
            updated_at=_dt_from_db(row["updated_at"]),
            execution_id=row["execution_id"],
            version=int(row["version"]),
            metadata=metadata,
        )

    def list_by_state(self, state: str) -> tuple[IdempotencyRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM idempotency_records WHERE state = ?",
            (state,),
        ).fetchall()
        out = []
        for row in rows:
            key = self._key_cache.get(row["key_hash"], row["key_hash"])
            metadata = _merge_payload(
                row["safe_metadata_json"],
                row["encrypted_payload_json"],
                encryption=self._encryption,
            )
            out.append(
                IdempotencyRecord(
                    key=key,
                    action_id=row["action_id"],
                    state=row["state"],
                    created_at=_dt_from_db(row["created_at"]),
                    updated_at=_dt_from_db(row["updated_at"]),
                    execution_id=row["execution_id"],
                    version=int(row["version"]),
                    metadata=metadata,
                )
            )
        return tuple(out)


class PersistentReconciliationStore:
    def __init__(
        self,
        connection: SqliteConnection,
        *,
        encryption: EncryptionService | None = None,
    ):
        self._connection = connection
        self._encryption = encryption

    def create(self, record: ReconciliationRecord) -> ReconciliationRecord:
        return self._upsert(record, insert=True)

    def get(self, reconciliation_id: str) -> ReconciliationRecord | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM reconciliations WHERE reconciliation_id = ?",
            (reconciliation_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def save(self, record: ReconciliationRecord) -> ReconciliationRecord:
        return self._upsert(record, insert=False)

    def find_by_execution(self, execution_id: str) -> tuple[ReconciliationRecord, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM reconciliations WHERE execution_id = ?",
            (execution_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_pending(self) -> tuple[ReconciliationRecord, ...]:
        from side_effects.models import RECONCILIATION_ACTIVE

        conn = self._connection.connect()
        placeholders = ",".join("?" for _ in RECONCILIATION_ACTIVE)
        rows = conn.execute(
            f"SELECT * FROM reconciliations WHERE status IN ({placeholders})",
            tuple(RECONCILIATION_ACTIVE),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_manual_review(self) -> tuple[ReconciliationRecord, ...]:
        from side_effects.models import RECON_MANUAL_REVIEW

        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM reconciliations WHERE status = ?",
            (RECON_MANUAL_REVIEW,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def _upsert(
        self, record: ReconciliationRecord, *, insert: bool
    ) -> ReconciliationRecord:
        safe_json, encrypted_json = _split_payload(
            dict(record.metadata),
            sensitivity=SENSITIVITY_INTERNAL,
            encryption=self._encryption,
        )
        params = (
            record.reconciliation_id,
            record.execution_id,
            record.workflow_id,
            record.task_id,
            record.action_id,
            record.tool_id,
            record.operation,
            record.idempotency_key_hash,
            record.status,
            record.decision,
            int(record.attempt),
            _dt_to_db(record.created_at),
            _dt_to_db(record.started_at),
            _dt_to_db(record.completed_at),
            _dt_to_db(record.last_checked_at),
            _dt_to_db(record.next_check_at),
            record.external_reference,
            record.reason_code,
            int(record.version),
            record.resolver_id,
            SENSITIVITY_INTERNAL,
            safe_json,
            encrypted_json,
        )
        conn = self._connection.connect()
        try:
            if insert:
                conn.execute(
                    """
                    INSERT INTO reconciliations (
                        reconciliation_id, execution_id, workflow_id, task_id, action_id,
                        tool_id, operation, idempotency_key_hash, status, decision, attempt,
                        created_at, started_at, completed_at, last_checked_at, next_check_at,
                        external_reference, reason_code, version, resolver_id, sensitivity,
                        safe_metadata_json, encrypted_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
            else:
                expected = int(record.version) - 1
                cur = conn.execute(
                    """
                    UPDATE reconciliations SET
                        execution_id=?, workflow_id=?, task_id=?, action_id=?, tool_id=?,
                        operation=?, idempotency_key_hash=?, status=?, decision=?, attempt=?,
                        created_at=?, started_at=?, completed_at=?, last_checked_at=?,
                        next_check_at=?, external_reference=?, reason_code=?, version=?,
                        resolver_id=?, sensitivity=?, safe_metadata_json=?, encrypted_payload_json=?
                    WHERE reconciliation_id=? AND version=?
                    """,
                    params[1:] + (record.reconciliation_id, expected),
                )
                if cur.rowcount != 1:
                    raise SideEffectPersistenceConflictError()
            self._connection.maybe_autocommit()
        except SideEffectPersistenceConflictError:
            raise
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "side_effect_persistence_unavailable"
            ) from exc
        return record

    def _from_row(self, row: sqlite3.Row) -> ReconciliationRecord:
        metadata = _merge_payload(
            row["safe_metadata_json"],
            row["encrypted_payload_json"],
            encryption=self._encryption,
        )
        return ReconciliationRecord(
            reconciliation_id=row["reconciliation_id"],
            execution_id=row["execution_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            action_id=row["action_id"],
            tool_id=row["tool_id"],
            operation=row["operation"],
            idempotency_key_hash=row["idempotency_key_hash"],
            status=row["status"],
            decision=row["decision"],
            attempt=int(row["attempt"]),
            created_at=_dt_from_db(row["created_at"]),
            started_at=_dt_from_db(row["started_at"]),
            completed_at=_dt_from_db(row["completed_at"]),
            last_checked_at=_dt_from_db(row["last_checked_at"]),
            next_check_at=_dt_from_db(row["next_check_at"]),
            external_reference=row["external_reference"],
            reason_code=row["reason_code"],
            version=int(row["version"]),
            resolver_id=row["resolver_id"],
            metadata=metadata,
        )
