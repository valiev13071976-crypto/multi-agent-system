"""Integration operation ledger — safe, tenant-scoped, no auth/payload secrets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from autonomy.models import sanitize_metadata
from integrations.errors import IdempotencyConflictError
from security.tenant import normalize_tenant_id

_DDL = """
CREATE TABLE IF NOT EXISTS integration_operation_ledger (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    integration_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    operation_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    request_fingerprint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    retries INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_integration_ledger_idem
ON integration_operation_ledger(tenant_id, integration_id, idempotency_key)
WHERE idempotency_key != '';
CREATE INDEX IF NOT EXISTS idx_integration_ledger_tenant
ON integration_operation_ledger(tenant_id, integration_id, started_at);
"""


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint_request(operation: str, body: dict | None) -> str:
    raw = json.dumps(
        {"op": operation, "body": sanitize_metadata(body or {})},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OperationLedger:
    def __init__(self, *, path: str | None = None, shared_connection=None):
        self._shared = shared_connection
        self._path = path or ":memory:"
        self._lock = threading.RLock()
        self._local = threading.local()
        self._owns = shared_connection is None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared.connect()
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _commit(self, conn) -> None:
        if self._shared is not None and hasattr(self._shared, "maybe_autocommit"):
            self._shared.maybe_autocommit()
            return
        conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_DDL)
            self._commit(conn)

    def begin(
        self,
        *,
        tenant_id: str,
        integration_id: str,
        operation_type: str,
        idempotency_key: str = "",
        request_fingerprint: str = "",
        workflow_id: str = "",
        task_id: str = "",
        request_id: str = "",
    ) -> dict:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM integration_operation_ledger "
                    "WHERE tenant_id=? AND integration_id=? AND idempotency_key=?",
                    (tenant, integration_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if (
                        request_fingerprint
                        and existing["request_fingerprint"]
                        and existing["request_fingerprint"] != request_fingerprint
                    ):
                        raise IdempotencyConflictError("idempotency_conflict")
                    return dict(existing)
            op_id = f"iop-{uuid.uuid4()}"
            now = _utc().isoformat()
            conn.execute(
                "INSERT INTO integration_operation_ledger("
                "operation_id, tenant_id, integration_id, workflow_id, task_id, request_id, "
                "operation_type, idempotency_key, request_fingerprint, status, started_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    op_id,
                    tenant,
                    integration_id,
                    workflow_id,
                    task_id,
                    request_id,
                    operation_type,
                    idempotency_key or "",
                    request_fingerprint or "",
                    "started",
                    now,
                ),
            )
            self._commit(conn)
            return {
                "operation_id": op_id,
                "tenant_id": tenant,
                "integration_id": integration_id,
                "status": "started",
                "idempotency_key": idempotency_key,
            }

    def complete(
        self,
        operation_id: str,
        *,
        tenant_id: str,
        status: str = "completed",
        external_id: str = "",
        error_code: str = "",
        result: dict | None = None,
        retries: int = 0,
    ) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE integration_operation_ledger SET status=?, external_id=?, error_code=?, "
                "result_json=?, retries=?, completed_at=? "
                "WHERE operation_id=? AND tenant_id=?",
                (
                    status,
                    external_id,
                    error_code,
                    json.dumps(sanitize_metadata(result or {}), separators=(",", ":"), sort_keys=True),
                    int(retries),
                    _utc().isoformat(),
                    operation_id,
                    tenant,
                ),
            )
            self._commit(conn)

    def get(self, operation_id: str, *, tenant_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM integration_operation_ledger WHERE operation_id=? AND tenant_id=?",
                (operation_id, tenant),
            ).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        if not self._owns:
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
