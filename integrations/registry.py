"""Tenant-scoped integration registry — metadata only, no secret values."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from autonomy.models import sanitize_metadata
from integrations.contracts import (
    CircuitBreakerPolicy,
    HEALTH_UNKNOWN,
    HealthPolicy,
    IntegrationDescriptor,
    IntegrationHealth,
    RetryPolicy,
    TimeoutPolicy,
)
from integrations.errors import IntegrationAccessDeniedError, IntegrationNotFoundError
from security.tenant import normalize_tenant_id

_DDL = """
CREATE TABLE IF NOT EXISTS integration_registry (
    tenant_id TEXT NOT NULL,
    integration_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    integration_type TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    environment TEXT NOT NULL DEFAULT 'production',
    auth_strategy TEXT NOT NULL,
    credential_ref TEXT NOT NULL DEFAULT '',
    read_capabilities_json TEXT NOT NULL DEFAULT '[]',
    write_capabilities_json TEXT NOT NULL DEFAULT '[]',
    allowed_operations_json TEXT NOT NULL DEFAULT '[]',
    health_policy_json TEXT NOT NULL DEFAULT '{}',
    timeout_policy_json TEXT NOT NULL DEFAULT '{}',
    retry_policy_json TEXT NOT NULL DEFAULT '{}',
    circuit_breaker_policy_json TEXT NOT NULL DEFAULT '{}',
    safe_settings_json TEXT NOT NULL DEFAULT '{}',
    base_url TEXT NOT NULL DEFAULT '',
    allowed_hosts_json TEXT NOT NULL DEFAULT '[]',
    ip_allowlist_json TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, integration_id)
);
CREATE INDEX IF NOT EXISTS idx_integration_registry_tenant
ON integration_registry(tenant_id, enabled);
CREATE INDEX IF NOT EXISTS idx_integration_registry_provider
ON integration_registry(tenant_id, provider, integration_type);

CREATE TABLE IF NOT EXISTS integration_health (
    tenant_id TEXT NOT NULL,
    integration_id TEXT NOT NULL,
    status TEXT NOT NULL,
    last_check TEXT,
    latency_ms REAL,
    error_code TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, integration_id)
);

CREATE TABLE IF NOT EXISTS integration_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    integration_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integration_audit_tenant
ON integration_audit(tenant_id, created_at);
"""


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _j(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _loads(raw: str, default):
    try:
        return json.loads(raw or "") or default
    except Exception:
        return default


def descriptor_to_row(d: IntegrationDescriptor) -> dict:
    return {
        "tenant_id": d.tenant_id,
        "integration_id": d.integration_id,
        "provider": d.provider,
        "integration_type": d.integration_type,
        "adapter_id": d.adapter_id,
        "enabled": 1 if d.enabled else 0,
        "environment": d.environment,
        "auth_strategy": d.auth_strategy,
        "credential_ref": d.credential_ref,
        "read_capabilities_json": _j(list(d.read_capabilities)),
        "write_capabilities_json": _j(list(d.write_capabilities)),
        "allowed_operations_json": _j(list(d.allowed_operations)),
        "health_policy_json": _j(
            {
                "check_interval_seconds": d.health_policy.check_interval_seconds,
                "timeout_seconds": d.health_policy.timeout_seconds,
                "require_auth_probe": d.health_policy.require_auth_probe,
            }
        ),
        "timeout_policy_json": _j(
            {
                "connect_seconds": d.timeout_policy.connect_seconds,
                "read_seconds": d.timeout_policy.read_seconds,
                "total_seconds": d.timeout_policy.total_seconds,
            }
        ),
        "retry_policy_json": _j(
            {
                "max_attempts": d.retry_policy.max_attempts,
                "backoff_seconds": d.retry_policy.backoff_seconds,
                "retry_on_status": list(d.retry_policy.retry_on_status),
                "retry_writes": d.retry_policy.retry_writes,
            }
        ),
        "circuit_breaker_policy_json": _j(
            {
                "failure_threshold": d.circuit_breaker_policy.failure_threshold,
                "window_seconds": d.circuit_breaker_policy.window_seconds,
                "cooldown_seconds": d.circuit_breaker_policy.cooldown_seconds,
                "half_open_probe_limit": d.circuit_breaker_policy.half_open_probe_limit,
            }
        ),
        "safe_settings_json": _j(dict(d.safe_settings)),
        "base_url": d.base_url,
        "allowed_hosts_json": _j(list(d.allowed_hosts)),
        "ip_allowlist_json": _j(list(d.ip_allowlist)),
        "version": d.version,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
    }


def row_to_descriptor(row) -> IntegrationDescriptor:
    hp = _loads(row["health_policy_json"], {})
    tp = _loads(row["timeout_policy_json"], {})
    rp = _loads(row["retry_policy_json"], {})
    cp = _loads(row["circuit_breaker_policy_json"], {})
    return IntegrationDescriptor(
        integration_id=row["integration_id"],
        tenant_id=row["tenant_id"],
        provider=row["provider"],
        integration_type=row["integration_type"],
        adapter_id=row["adapter_id"],
        enabled=bool(row["enabled"]),
        environment=row["environment"],
        auth_strategy=row["auth_strategy"],
        credential_ref=row["credential_ref"] or "",
        read_capabilities=tuple(_loads(row["read_capabilities_json"], [])),
        write_capabilities=tuple(_loads(row["write_capabilities_json"], [])),
        allowed_operations=tuple(_loads(row["allowed_operations_json"], [])),
        health_policy=HealthPolicy(
            check_interval_seconds=float(hp.get("check_interval_seconds", 60)),
            timeout_seconds=float(hp.get("timeout_seconds", 5)),
            require_auth_probe=bool(hp.get("require_auth_probe", False)),
        ),
        timeout_policy=TimeoutPolicy(
            connect_seconds=float(tp.get("connect_seconds", 5)),
            read_seconds=float(tp.get("read_seconds", 15)),
            total_seconds=float(tp.get("total_seconds", 30)),
        ),
        retry_policy=RetryPolicy(
            max_attempts=int(rp.get("max_attempts", 3)),
            backoff_seconds=float(rp.get("backoff_seconds", 0.5)),
            retry_on_status=tuple(rp.get("retry_on_status") or (429, 500, 502, 503, 504)),
            retry_writes=bool(rp.get("retry_writes", False)),
        ),
        circuit_breaker_policy=CircuitBreakerPolicy(
            failure_threshold=int(cp.get("failure_threshold", 5)),
            window_seconds=float(cp.get("window_seconds", 60)),
            cooldown_seconds=float(cp.get("cooldown_seconds", 30)),
            half_open_probe_limit=int(cp.get("half_open_probe_limit", 1)),
        ),
        safe_settings=_loads(row["safe_settings_json"], {}),
        base_url=row["base_url"] or "",
        allowed_hosts=tuple(_loads(row["allowed_hosts_json"], [])),
        ip_allowlist=tuple(_loads(row["ip_allowlist_json"], [])),
        version=int(row["version"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class IntegrationRegistry:
    """Canonical tenant-scoped registry/config store."""

    persistence_backend = "sqlite"

    def __init__(self, *, path: str | None = None, shared_connection=None):
        self._shared = shared_connection
        self._path = path
        self._lock = threading.RLock()
        self._local = threading.local()
        self._owns = shared_connection is None
        if shared_connection is None and not path:
            self._path = ":memory:"
        self.connection_mode = "shared" if shared_connection else "dedicated"
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

    def _commit(self, conn: sqlite3.Connection) -> None:
        if self._shared is not None and hasattr(self._shared, "maybe_autocommit"):
            self._shared.maybe_autocommit()
            return
        conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_DDL)
            self._commit(conn)

    def register(self, descriptor: IntegrationDescriptor, *, actor: str = "") -> IntegrationDescriptor:
        row = descriptor_to_row(descriptor)
        with self._lock:
            conn = self._connect()
            existing = conn.execute(
                "SELECT version FROM integration_registry WHERE tenant_id=? AND integration_id=?",
                (descriptor.tenant_id, descriptor.integration_id),
            ).fetchone()
            if existing is not None:
                row["version"] = int(existing["version"]) + 1
                row["updated_at"] = _utc().isoformat()
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT OR REPLACE INTO integration_registry ({cols}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            self._audit(
                conn,
                tenant_id=descriptor.tenant_id,
                integration_id=descriptor.integration_id,
                event_type="integration_created" if existing is None else "integration_updated",
                actor=actor,
                details={"version": row["version"], "enabled": descriptor.enabled},
            )
            self._commit(conn)
            return self.get(descriptor.tenant_id, descriptor.integration_id)

    def enable(self, tenant_id: str, integration_id: str, *, enabled: bool, actor: str = "") -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE integration_registry SET enabled=?, updated_at=?, version=version+1 "
                "WHERE tenant_id=? AND integration_id=?",
                (1 if enabled else 0, _utc().isoformat(), tenant, integration_id),
            )
            if cur.rowcount == 0:
                raise IntegrationNotFoundError("integration_not_found")
            self._audit(
                conn,
                tenant_id=tenant,
                integration_id=integration_id,
                event_type="integration_enabled" if enabled else "integration_disabled",
                actor=actor,
                details={},
            )
            self._commit(conn)

    def rotate_credential_ref(
        self, tenant_id: str, integration_id: str, credential_ref: str, *, actor: str = ""
    ) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE integration_registry SET credential_ref=?, updated_at=?, version=version+1 "
                "WHERE tenant_id=? AND integration_id=?",
                (credential_ref, _utc().isoformat(), tenant, integration_id),
            )
            if cur.rowcount == 0:
                raise IntegrationNotFoundError("integration_not_found")
            self._audit(
                conn,
                tenant_id=tenant,
                integration_id=integration_id,
                event_type="credential_ref_changed",
                actor=actor,
                details={"credential_ref": credential_ref},
            )
            self._commit(conn)

    def get(self, tenant_id: str, integration_id: str) -> IntegrationDescriptor | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM integration_registry WHERE tenant_id=? AND integration_id=?",
                (tenant, integration_id),
            ).fetchone()
            return row_to_descriptor(row) if row else None

    def assert_access(self, tenant_id: str, integration_id: str) -> IntegrationDescriptor:
        desc = self.get(tenant_id, integration_id)
        if desc is None:
            raise IntegrationAccessDeniedError("integration_access_denied")
        return desc

    def list_for_tenant(self, tenant_id: str) -> list[IntegrationDescriptor]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM integration_registry WHERE tenant_id=? ORDER BY integration_id",
                (tenant,),
            ).fetchall()
            return [row_to_descriptor(r) for r in rows]

    def lookup(
        self,
        tenant_id: str,
        *,
        provider: str | None = None,
        integration_type: str | None = None,
        capability: str | None = None,
        enabled_only: bool = True,
    ) -> list[IntegrationDescriptor]:
        items = self.list_for_tenant(tenant_id)
        out = []
        for d in items:
            if enabled_only and not d.enabled:
                continue
            if provider and d.provider != provider:
                continue
            if integration_type and d.integration_type != integration_type:
                continue
            if capability and capability not in d.read_capabilities and capability not in d.write_capabilities:
                continue
            out.append(d)
        return out

    def set_health(self, tenant_id: str, integration_id: str, health: IntegrationHealth) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO integration_health("
                "tenant_id, integration_id, status, last_check, latency_ms, error_code, "
                "consecutive_failures, details_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    integration_id,
                    health.status,
                    health.last_check.isoformat() if health.last_check else None,
                    health.latency_ms,
                    health.error_code,
                    health.consecutive_failures,
                    _j(dict(health.details)),
                ),
            )
            self._commit(conn)

    def get_health(self, tenant_id: str, integration_id: str) -> IntegrationHealth:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM integration_health WHERE tenant_id=? AND integration_id=?",
                (tenant, integration_id),
            ).fetchone()
            if row is None:
                return IntegrationHealth(status=HEALTH_UNKNOWN)
            return IntegrationHealth(
                status=row["status"],
                last_check=datetime.fromisoformat(row["last_check"]) if row["last_check"] else None,
                latency_ms=row["latency_ms"],
                error_code=row["error_code"] or "",
                consecutive_failures=int(row["consecutive_failures"] or 0),
                details=_loads(row["details_json"], {}),
            )

    def audit_events(self, tenant_id: str, *, limit: int = 100) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT event_type, integration_id, actor, details_json, created_at "
                "FROM integration_audit WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant, int(limit)),
            ).fetchall()
            return [
                {
                    "event_type": r["event_type"],
                    "integration_id": r["integration_id"],
                    "actor": r["actor"],
                    "details": sanitize_metadata(_loads(r["details_json"], {})),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def _audit(
        self,
        conn,
        *,
        tenant_id: str,
        integration_id: str,
        event_type: str,
        actor: str,
        details: dict,
    ) -> None:
        conn.execute(
            "INSERT INTO integration_audit(tenant_id, integration_id, event_type, actor, details_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                tenant_id,
                integration_id,
                event_type,
                actor,
                _j(sanitize_metadata(details)),
                _utc().isoformat(),
            ),
        )

    def scan_plaintext_violations(self) -> list[str]:
        """Startup scan — flag forbidden secret-like keys in safe_settings."""
        forbidden = {
            "api_key",
            "token",
            "password",
            "secret",
            "authorization",
            "access_token",
            "refresh_token",
            "client_secret",
            "private_key",
        }
        violations = []
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT tenant_id, integration_id, safe_settings_json FROM integration_registry"
            ).fetchall()
            for row in rows:
                settings = _loads(row["safe_settings_json"], {})
                for key in settings:
                    if str(key).lower() in forbidden:
                        violations.append(
                            f"{row['tenant_id']}/{row['integration_id']}:{key}"
                        )
        return violations

    def close(self) -> None:
        if not self._owns:
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
