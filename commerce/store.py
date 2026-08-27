"""Tenant-scoped commerce orchestration persistence — not a local ERP."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id

_DDL = """
CREATE TABLE IF NOT EXISTS commerce_orders (
    tenant_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fulfillment_state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, order_id)
);
CREATE TABLE IF NOT EXISTS commerce_declarations (
    tenant_id TEXT NOT NULL,
    declaration_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    immutable INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, declaration_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_decl_order
ON commerce_declarations(tenant_id, order_id);
CREATE TABLE IF NOT EXISTS commerce_suppliers (
    tenant_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, supplier_id)
);
CREATE TABLE IF NOT EXISTS commerce_ops (
    tenant_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, operation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_ops_idem
ON commerce_ops(tenant_id, idempotency_key) WHERE idempotency_key != '';
CREATE TABLE IF NOT EXISTS commerce_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    order_id TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commerce_audit_tenant ON commerce_audit(tenant_id, created_at);
CREATE TABLE IF NOT EXISTS commerce_reconcile (
    tenant_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, finding_id)
);
CREATE INDEX IF NOT EXISTS idx_commerce_reconcile_tenant
ON commerce_reconcile(tenant_id, checked_at);
CREATE TABLE IF NOT EXISTS commerce_rules_used (
    tenant_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, order_id, rule_id, rule_version)
);
"""


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _j(obj: Any) -> str:
    return json.dumps(sanitize_metadata(obj if isinstance(obj, dict) else {"value": obj}), separators=(",", ":"), sort_keys=True)


class CommerceStore:
    persistence_backend = "sqlite"

    def __init__(self, *, path: str | None = None, shared_connection=None):
        self._shared = shared_connection
        self._path = path or ":memory:"
        self._lock = threading.RLock()
        self._local = threading.local()
        self._owns = shared_connection is None
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
            self._ensure_reconcile_columns(conn)
            self._commit(conn)

    def _ensure_reconcile_columns(self, conn) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(commerce_reconcile)").fetchall()
        }
        for name, col_type, default in (
            ("status", "TEXT", "''"),
            ("run_id", "TEXT", "''"),
            ("workflow_id", "TEXT", "''"),
            ("order_id", "TEXT", "''"),
            ("checked_at", "TEXT", "''"),
        ):
            if name in existing:
                continue
            conn.execute(
                f"ALTER TABLE commerce_reconcile ADD COLUMN {name} {col_type} NOT NULL DEFAULT {default}"
            )

    def list_tenant_ids(self) -> list[str]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT DISTINCT tenant_id FROM commerce_orders ORDER BY tenant_id"
            ).fetchall()
            return [str(r["tenant_id"]) for r in rows]

    def list_order_ids(self, tenant_id: str) -> list[str]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT order_id FROM commerce_orders WHERE tenant_id=? ORDER BY updated_at DESC",
                (tenant,),
            ).fetchall()
            return [str(r["order_id"]) for r in rows]

    def save_reconcile_finding(
        self,
        tenant_id: str,
        finding_id: str,
        severity: str,
        payload: dict,
        *,
        status: str = "",
        run_id: str = "",
        workflow_id: str = "",
        order_id: str = "",
        checked_at: str | None = None,
    ) -> None:
        tenant = normalize_tenant_id(tenant_id)
        stamp = checked_at or _utc().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO commerce_reconcile("
                "tenant_id, finding_id, severity, status, run_id, workflow_id, order_id, "
                "payload_json, checked_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    finding_id,
                    severity,
                    status or severity,
                    run_id,
                    workflow_id,
                    order_id,
                    _j(payload),
                    stamp,
                    stamp,
                ),
            )
            self._commit(conn)

    def get_reconcile_finding(self, tenant_id: str, finding_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM commerce_reconcile WHERE tenant_id=? AND finding_id=?",
                (tenant, finding_id),
            ).fetchone()
            if row is None:
                return None
            data = json.loads(row["payload_json"])
            data.update(
                {
                    "finding_id": row["finding_id"],
                    "severity": row["severity"],
                    "status": row["status"] or row["severity"],
                    "run_id": row["run_id"],
                    "workflow_id": row["workflow_id"],
                    "order_id": row["order_id"],
                    "checked_at": row["checked_at"],
                }
            )
            return data

    def list_reconcile(self, tenant_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT finding_id, severity, status, run_id, workflow_id, order_id, "
                "payload_json, checked_at FROM commerce_reconcile WHERE tenant_id=? "
                "ORDER BY checked_at DESC",
                (tenant,),
            ).fetchall()
            out = []
            for r in rows:
                item = json.loads(r["payload_json"])
                item.update(
                    {
                        "finding_id": r["finding_id"],
                        "severity": r["severity"],
                        "status": r["status"] or r["severity"],
                        "run_id": r["run_id"],
                        "workflow_id": r["workflow_id"],
                        "order_id": r["order_id"],
                        "checked_at": r["checked_at"],
                    }
                )
                out.append(item)
            return out

    def save_order(self, tenant_id: str, order_id: str, payload: dict, state: str) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO commerce_orders(tenant_id, order_id, payload_json, fulfillment_state, updated_at) "
                "VALUES (?,?,?,?,?)",
                (tenant, order_id, _j(payload), state, _utc().isoformat()),
            )
            self._commit(conn)

    def get_order(self, tenant_id: str, order_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json, fulfillment_state FROM commerce_orders WHERE tenant_id=? AND order_id=?",
                (tenant, order_id),
            ).fetchone()
            if row is None:
                return None
            data = json.loads(row["payload_json"])
            data["fulfillment_state"] = row["fulfillment_state"]
            return data

    def save_declaration(self, tenant_id: str, declaration_id: str, order_id: str, payload: dict) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO commerce_declarations(tenant_id, declaration_id, order_id, payload_json, immutable, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (tenant, declaration_id, order_id, _j(payload), _utc().isoformat()),
            )
            self._commit(conn)

    def get_declaration(self, tenant_id: str, declaration_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM commerce_declarations WHERE tenant_id=? AND declaration_id=?",
                (tenant, declaration_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def get_declaration_for_order(self, tenant_id: str, order_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM commerce_declarations WHERE tenant_id=? AND order_id=?",
                (tenant, order_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def save_supplier(self, tenant_id: str, supplier_id: str, payload: dict, *, active: bool = True) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO commerce_suppliers(tenant_id, supplier_id, payload_json, active) VALUES (?,?,?,?)",
                (tenant, supplier_id, _j(payload), 1 if active else 0),
            )
            self._commit(conn)

    def get_supplier(self, tenant_id: str, supplier_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM commerce_suppliers WHERE tenant_id=? AND supplier_id=?",
                (tenant, supplier_id),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def list_suppliers(self, tenant_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT payload_json FROM commerce_suppliers WHERE tenant_id=? AND active=1",
                (tenant,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def begin_op(self, tenant_id: str, operation_id: str, kind: str, idempotency_key: str, payload: dict) -> dict | None:
        """Return existing op if idempotency hits; else insert started."""
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            if idempotency_key:
                row = conn.execute(
                    "SELECT payload_json, status, operation_id FROM commerce_ops WHERE tenant_id=? AND idempotency_key=?",
                    (tenant, idempotency_key),
                ).fetchone()
                if row is not None:
                    data = json.loads(row["payload_json"])
                    data["status"] = row["status"]
                    data["operation_id"] = row["operation_id"]
                    return data
            conn.execute(
                "INSERT INTO commerce_ops(tenant_id, operation_id, idempotency_key, kind, payload_json, status, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (tenant, operation_id, idempotency_key or "", kind, _j(payload), "started", _utc().isoformat()),
            )
            self._commit(conn)
            return None

    def complete_op(self, tenant_id: str, operation_id: str, status: str, payload: dict) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE commerce_ops SET status=?, payload_json=? WHERE tenant_id=? AND operation_id=?",
                (status, _j(payload), tenant, operation_id),
            )
            self._commit(conn)

    def audit(self, tenant_id: str, event_type: str, *, order_id: str = "", details: dict | None = None) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO commerce_audit(tenant_id, event_type, order_id, details_json, created_at) VALUES (?,?,?,?,?)",
                (tenant, event_type, order_id, _j(details or {}), _utc().isoformat()),
            )
            self._commit(conn)

    def list_audit(self, tenant_id: str, *, limit: int = 100) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT event_type, order_id, details_json, created_at FROM commerce_audit "
                "WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant, int(limit)),
            ).fetchall()
            return [
                {
                    "event_type": r["event_type"],
                    "order_id": r["order_id"],
                    "details": json.loads(r["details_json"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def record_rule_used(self, tenant_id: str, order_id: str, rule_id: str, rule_version: str) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR IGNORE INTO commerce_rules_used(tenant_id, order_id, rule_id, rule_version, decided_at) "
                "VALUES (?,?,?,?,?)",
                (tenant, order_id, rule_id, rule_version, _utc().isoformat()),
            )
            self._commit(conn)

    def rules_used_for_order(self, tenant_id: str, order_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT rule_id, rule_version, decided_at FROM commerce_rules_used WHERE tenant_id=? AND order_id=?",
                (tenant, order_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def close(self) -> None:
        if not self._owns:
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
