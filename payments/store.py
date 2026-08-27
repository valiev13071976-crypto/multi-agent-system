"""Tenant-scoped payments persistence — synchronized state only, no card data."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autonomy.models import sanitize_metadata
from payments.contracts import assert_no_card_data
from security.tenant import normalize_tenant_id

_DDL = """
CREATE TABLE IF NOT EXISTS payments_records (
    tenant_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_transaction_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, payment_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_ext
ON payments_records(tenant_id, external_transaction_id)
WHERE external_transaction_id != '';
CREATE TABLE IF NOT EXISTS payments_bank_tx (
    tenant_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    external_bank_id TEXT NOT NULL DEFAULT '',
    account_ref TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    source_statement_ref TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, transaction_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_ext
ON payments_bank_tx(tenant_id, external_bank_id)
WHERE external_bank_id != '';
CREATE TABLE IF NOT EXISTS payments_allocations (
    tenant_id TEXT NOT NULL,
    allocation_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    order_id TEXT NOT NULL DEFAULT '',
    invoice_id TEXT NOT NULL DEFAULT '',
    allocated_amount REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, allocation_id)
);
CREATE INDEX IF NOT EXISTS idx_alloc_payment
ON payments_allocations(tenant_id, payment_id);
CREATE INDEX IF NOT EXISTS idx_alloc_order
ON payments_allocations(tenant_id, order_id);
CREATE TABLE IF NOT EXISTS payments_matches (
    tenant_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    payment_id TEXT NOT NULL DEFAULT '',
    bank_transaction_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, match_id)
);
CREATE TABLE IF NOT EXISTS payments_refunds (
    tenant_id TEXT NOT NULL,
    refund_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    status TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, refund_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refund_idem
ON payments_refunds(tenant_id, idempotency_key)
WHERE idempotency_key != '';
CREATE TABLE IF NOT EXISTS payments_findings (
    tenant_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    workflow_ref TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, finding_id)
);
CREATE TABLE IF NOT EXISTS payments_events (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id)
);
CREATE TABLE IF NOT EXISTS payments_ops (
    tenant_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, operation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_ops_idem
ON payments_ops(tenant_id, idempotency_key) WHERE idempotency_key != '';
CREATE TABLE IF NOT EXISTS payments_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    refs_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_audit_tenant ON payments_audit(tenant_id, created_at);
CREATE TABLE IF NOT EXISTS payments_statements (
    tenant_id TEXT NOT NULL,
    statement_ref TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    opening REAL,
    closing REAL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, statement_ref)
);
CREATE TABLE IF NOT EXISTS payments_targets (
    tenant_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, order_id)
);
"""


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _j(obj: Any) -> str:
    return json.dumps(
        sanitize_metadata(obj if isinstance(obj, dict) else {"value": obj}),
        separators=(",", ":"),
        sort_keys=True,
    )


class PaymentsStore:
    persistence_backend = "sqlite"

    def __init__(self, *, path: str | None = None, shared_connection=None):
        self._shared = shared_connection
        self._path = path or ":memory:"
        self._lock = threading.RLock()
        self._local = threading.local()
        self.connection_mode = "shared" if shared_connection else "dedicated"
        self._init()

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

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_DDL)
            self._commit(conn)

    def close(self) -> None:
        if self._shared is not None:
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def list_tenant_ids(self) -> list[str]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT DISTINCT tenant_id FROM payments_records ORDER BY tenant_id"
            ).fetchall()
            return [str(r["tenant_id"]) for r in rows]

    def save_payment(self, tenant_id: str, payment_id: str, payload: dict, status: str) -> None:
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO payments_records("
                "tenant_id, payment_id, provider, external_transaction_id, status, amount, "
                "currency, payload_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, "
                "COALESCE((SELECT created_at FROM payments_records WHERE tenant_id=? AND payment_id=?), ?), ?)",
                (
                    tenant,
                    payment_id,
                    str(payload.get("provider") or ""),
                    str(payload.get("external_transaction_id") or ""),
                    status,
                    float(payload.get("amount") or 0),
                    str(payload.get("currency") or "RUB"),
                    _j(payload),
                    tenant,
                    payment_id,
                    stamp,
                    stamp,
                ),
            )
            self._commit(conn)

    def get_payment(self, tenant_id: str, payment_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM payments_records WHERE tenant_id=? AND payment_id=?",
                (tenant, payment_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def get_payment_by_external(self, tenant_id: str, external_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM payments_records WHERE tenant_id=? AND external_transaction_id=?",
                (tenant, external_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def list_payments(self, tenant_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT payload_json FROM payments_records WHERE tenant_id=? ORDER BY updated_at DESC",
                (tenant,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def save_bank_tx(self, tenant_id: str, transaction_id: str, payload: dict) -> bool:
        """Return False if duplicate external_bank_id (already imported)."""
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        ext = str(payload.get("external_bank_id") or "")
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            if ext:
                existing = conn.execute(
                    "SELECT transaction_id FROM payments_bank_tx WHERE tenant_id=? AND external_bank_id=?",
                    (tenant, ext),
                ).fetchone()
                if existing is not None:
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO payments_bank_tx("
                "tenant_id, transaction_id, external_bank_id, account_ref, amount, currency, "
                "source_statement_ref, payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    transaction_id,
                    ext,
                    str(payload.get("account_ref") or ""),
                    float(payload.get("amount") or 0),
                    str(payload.get("currency") or "RUB"),
                    str(payload.get("source_statement_ref") or ""),
                    _j(payload),
                    stamp,
                ),
            )
            self._commit(conn)
            return True

    def list_bank_tx(self, tenant_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT payload_json FROM payments_bank_tx WHERE tenant_id=? ORDER BY created_at DESC",
                (tenant,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def save_allocation(self, tenant_id: str, allocation_id: str, payload: dict) -> None:
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO payments_allocations("
                "tenant_id, allocation_id, payment_id, order_id, invoice_id, allocated_amount, "
                "currency, status, payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    allocation_id,
                    str(payload.get("payment_id") or ""),
                    str(payload.get("order_id") or ""),
                    str(payload.get("invoice_id") or ""),
                    float(payload.get("allocated_amount") or 0),
                    str(payload.get("currency") or "RUB"),
                    str(payload.get("status") or "CONFIRMED"),
                    _j(payload),
                    stamp,
                ),
            )
            self._commit(conn)

    def supersede_allocation(
        self, tenant_id: str, allocation_id: str, *, superseded_by: str
    ) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM payments_allocations WHERE tenant_id=? AND allocation_id=?",
                (tenant, allocation_id),
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload_json"])
            payload["status"] = "SUPERSEDED"
            payload["superseded_by"] = superseded_by
            conn.execute(
                "UPDATE payments_allocations SET status=?, payload_json=? "
                "WHERE tenant_id=? AND allocation_id=?",
                ("SUPERSEDED", _j(payload), tenant, allocation_id),
            )
            self._commit(conn)

    def list_allocations(
        self, tenant_id: str, *, payment_id: str = "", order_id: str = ""
    ) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            sql = "SELECT payload_json FROM payments_allocations WHERE tenant_id=?"
            args: list = [tenant]
            if payment_id:
                sql += " AND payment_id=?"
                args.append(payment_id)
            if order_id:
                sql += " AND order_id=?"
                args.append(order_id)
            sql += " ORDER BY created_at ASC"
            rows = conn.execute(sql, tuple(args)).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def save_match(self, tenant_id: str, match_id: str, payload: dict) -> None:
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO payments_matches("
                "tenant_id, match_id, payment_id, bank_transaction_id, payload_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    tenant,
                    match_id,
                    str(payload.get("payment_id") or ""),
                    str(payload.get("bank_transaction_id") or ""),
                    _j(payload),
                    stamp,
                ),
            )
            self._commit(conn)

    def save_refund(self, tenant_id: str, refund_id: str, payload: dict, status: str) -> None:
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO payments_refunds("
                "tenant_id, refund_id, payment_id, status, amount, currency, idempotency_key, "
                "payload_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, "
                "COALESCE((SELECT created_at FROM payments_refunds WHERE tenant_id=? AND refund_id=?), ?), ?)",
                (
                    tenant,
                    refund_id,
                    str(payload.get("payment_id") or ""),
                    status,
                    float(payload.get("amount") or 0),
                    str(payload.get("currency") or "RUB"),
                    str(payload.get("idempotency_key") or ""),
                    _j(payload),
                    tenant,
                    refund_id,
                    stamp,
                    stamp,
                ),
            )
            self._commit(conn)

    def get_refund(self, tenant_id: str, refund_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM payments_refunds WHERE tenant_id=? AND refund_id=?",
                (tenant, refund_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def get_refund_by_idempotency(self, tenant_id: str, key: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM payments_refunds WHERE tenant_id=? AND idempotency_key=?",
                (tenant, key),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def list_refunds(self, tenant_id: str, *, payment_id: str = "") -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            if payment_id:
                rows = conn.execute(
                    "SELECT payload_json FROM payments_refunds WHERE tenant_id=? AND payment_id=?",
                    (tenant, payment_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM payments_refunds WHERE tenant_id=?",
                    (tenant,),
                ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def save_finding(self, tenant_id: str, finding_id: str, payload: dict) -> None:
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO payments_findings("
                "tenant_id, finding_id, finding_type, severity, status, payload_json, created_at, "
                "resolved_at, workflow_ref) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    finding_id,
                    str(payload.get("finding_type") or ""),
                    str(payload.get("severity") or ""),
                    str(payload.get("status") or ""),
                    _j(payload),
                    stamp,
                    str(payload.get("resolved_at") or ""),
                    str(payload.get("workflow_ref") or ""),
                ),
            )
            self._commit(conn)

    def list_findings(self, tenant_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT payload_json FROM payments_findings WHERE tenant_id=? ORDER BY created_at DESC",
                (tenant,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def get_finding(self, tenant_id: str, finding_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM payments_findings WHERE tenant_id=? AND finding_id=?",
                (tenant, finding_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def save_event(self, tenant_id: str, event_id: str, event_type: str, payload: dict) -> bool:
        """False if duplicate event_id."""
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            existing = conn.execute(
                "SELECT event_id FROM payments_events WHERE tenant_id=? AND event_id=?",
                (tenant, event_id),
            ).fetchone()
            if existing is not None:
                return False
            conn.execute(
                "INSERT INTO payments_events("
                "tenant_id, event_id, event_type, provider, payload_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    tenant,
                    event_id,
                    event_type,
                    str(payload.get("provider") or ""),
                    _j(payload),
                    stamp,
                ),
            )
            self._commit(conn)
            return True

    def begin_op(
        self, tenant_id: str, operation_id: str, kind: str, idempotency_key: str, payload: dict
    ) -> dict | None:
        """Return existing op payload if idempotency hit, else None after insert."""
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            if idempotency_key:
                row = conn.execute(
                    "SELECT payload_json, status FROM payments_ops WHERE tenant_id=? AND idempotency_key=?",
                    (tenant, idempotency_key),
                ).fetchone()
                if row is not None:
                    data = json.loads(row["payload_json"])
                    data["_status"] = row["status"]
                    return data
            conn.execute(
                "INSERT INTO payments_ops("
                "tenant_id, operation_id, kind, idempotency_key, status, payload_json, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (tenant, operation_id, kind, idempotency_key or "", "started", _j(payload), stamp),
            )
            self._commit(conn)
            return None

    def complete_op(self, tenant_id: str, operation_id: str, status: str, payload: dict) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE payments_ops SET status=?, payload_json=? WHERE tenant_id=? AND operation_id=?",
                (status, _j(payload), tenant, operation_id),
            )
            self._commit(conn)

    def audit(self, tenant_id: str, event_type: str, *, refs: dict | None = None, details: dict | None = None) -> None:
        assert_no_card_data(refs)
        assert_no_card_data(details)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO payments_audit(tenant_id, event_type, refs_json, details_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (tenant, event_type, _j(refs or {}), _j(details or {}), stamp),
            )
            self._commit(conn)

    def list_audit(self, tenant_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT event_type, refs_json, details_json, created_at FROM payments_audit "
                "WHERE tenant_id=? ORDER BY id ASC",
                (tenant,),
            ).fetchall()
            return [
                {
                    "event_type": r["event_type"],
                    "refs": json.loads(r["refs_json"]),
                    "details": json.loads(r["details_json"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def save_statement(self, tenant_id: str, statement_ref: str, payload: dict) -> None:
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO payments_statements("
                "tenant_id, statement_ref, account_ref, period_start, period_end, opening, closing, "
                "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    statement_ref,
                    str(payload.get("account_ref") or ""),
                    str(payload.get("period_start") or ""),
                    str(payload.get("period_end") or ""),
                    payload.get("opening"),
                    payload.get("closing"),
                    _j(payload),
                    stamp,
                ),
            )
            self._commit(conn)

    def save_target(self, tenant_id: str, order_id: str, payload: dict) -> None:
        assert_no_card_data(payload)
        tenant = normalize_tenant_id(tenant_id)
        stamp = _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO payments_targets(tenant_id, order_id, payload_json, updated_at) "
                "VALUES (?,?,?,?)",
                (tenant, order_id, _j(payload), stamp),
            )
            self._commit(conn)

    def list_targets(self, tenant_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT payload_json FROM payments_targets WHERE tenant_id=?",
                (tenant,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def get_target(self, tenant_id: str, order_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM payments_targets WHERE tenant_id=? AND order_id=?",
                (tenant, order_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None
