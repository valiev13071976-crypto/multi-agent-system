"""Append-only admin audit store."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from operations_admin.models import AuditEventView
from security.redaction import redact


class AdminAuditStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_events (
                event_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                actor_ref TEXT NOT NULL,
                tenant_scope TEXT NOT NULL,
                capability TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                result TEXT NOT NULL,
                reason TEXT,
                request_id TEXT,
                execution_id TEXT,
                before_ref TEXT,
                after_ref TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit_events(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit_events(actor_ref);
            CREATE INDEX IF NOT EXISTS idx_admin_audit_tenant ON admin_audit_events(tenant_scope);
            """
        )
        self._conn.commit()

    def append(
        self,
        *,
        actor_ref: str,
        tenant_scope: str,
        capability: str,
        action: str,
        target_type: str,
        target_id: str,
        result: str,
        reason: str | None = None,
        request_id: str | None = None,
        execution_id: str | None = None,
        before_ref: str | None = None,
        after_ref: str | None = None,
    ) -> AuditEventView:
        event_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
        safe_reason = redact(reason or "")[:500] if reason else None
        self._conn.execute(
            """
            INSERT INTO admin_audit_events
            (event_id, timestamp, actor_ref, tenant_scope, capability, action,
             target_type, target_id, result, reason, request_id, execution_id,
             before_ref, after_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                ts,
                actor_ref,
                tenant_scope,
                capability,
                action,
                target_type,
                target_id,
                result,
                safe_reason,
                request_id,
                execution_id,
                before_ref,
                after_ref,
            ),
        )
        self._conn.commit()
        return AuditEventView(
            event_id=event_id,
            timestamp=ts,
            actor_ref=actor_ref,
            tenant_scope=tenant_scope,
            capability=capability,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            reason=safe_reason,
            request_id=request_id,
        )

    def list_events(
        self,
        *,
        tenant_scope: str | None = None,
        action: str | None = None,
        actor_ref: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditEventView], int]:
        limit = min(max(1, limit), 200)
        offset = max(0, offset)
        where = []
        params: list[object] = []
        if tenant_scope:
            where.append("tenant_scope = ?")
            params.append(tenant_scope)
        if action:
            where.append("action = ?")
            params.append(action)
        if actor_ref:
            where.append("actor_ref = ?")
            params.append(actor_ref)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM admin_audit_events{clause}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"""
            SELECT * FROM admin_audit_events{clause}
            ORDER BY timestamp DESC LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        items = [
            AuditEventView(
                event_id=r["event_id"],
                timestamp=r["timestamp"],
                actor_ref=r["actor_ref"],
                tenant_scope=r["tenant_scope"],
                capability=r["capability"],
                action=r["action"],
                target_type=r["target_type"],
                target_id=r["target_id"],
                result=r["result"],
                reason=r["reason"],
                request_id=r["request_id"],
            )
            for r in rows
        ]
        return items, int(total)

    def close(self) -> None:
        self._conn.close()
