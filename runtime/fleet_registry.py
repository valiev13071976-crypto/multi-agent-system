"""Shared runtime health / fleet registry (Scale 3.29).

Multiple application/worker instances must be able to see each other's
health without relying on process-local memory. This module distinguishes:

- LOCAL process state: whatever this instance already tracks in-process
  (e.g. ``WorkflowRuntimeBundle.concurrency_snapshot()``).
- SHARED fleet/runtime state: a durable, cross-instance-visible row per
  instance, written on every heartbeat and readable by any instance.

Staleness: an instance that stops heartbeating is never treated as healthy
forever. Every read computes ``is_stale`` from a bounded TTL relative to the
read-time clock, not from the writer's own belief about liveness.

Persistence follows the same self-contained pattern as
``providers.governor.SqliteProviderGovernorStore``: its own WAL-mode SQLite
file/connection (multi-process safe), independent of the side_effects schema
migration pipeline. No second unrelated persistence stack is introduced --
this reuses the exact same sqlite3 + WAL + BEGIN IMMEDIATE technique already
proven for distributed governor state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Sequence

DEFAULT_STALE_AFTER_SECONDS = 45.0
DEFAULT_DB_PATH = "./data/runtime_fleet.sqlite3"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_db(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class FleetRegistryConfig:
    """Bounded freshness policy: safe defaults, typed/validated values."""

    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
    enabled: bool = True

    @classmethod
    def from_env(cls, env: Mapping | None = None) -> "FleetRegistryConfig":
        source = env if env is not None else os.environ

        def _float(name: str, default: float) -> float:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                value = float(str(raw).strip())
            except ValueError:
                return default
            return value if value > 0 else default

        def _bool(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            stale_after_seconds=_float(
                "RUNTIME_FLEET_STALE_AFTER_SECONDS", DEFAULT_STALE_AFTER_SECONDS
            ),
            enabled=_bool("RUNTIME_FLEET_REGISTRY_ENABLED", True),
        )


@dataclass(frozen=True)
class InstanceHealthView:
    """Read-time view of one instance's shared health row.

    ``is_stale`` is always computed against the reader's own clock, never
    trusted from the writer -- a dead instance's old row is never reported
    healthy forever.
    """

    instance_id: str
    pool_name: str
    lanes: tuple[str, ...]
    runtime_role: str
    runtime_version: str
    draining: bool
    active_jobs: int
    max_concurrency: int
    available_concurrency: int
    queue_pressure: Mapping[str, Any]
    started_at: datetime | None
    heartbeat_at: datetime | None
    is_stale: bool
    age_seconds: float | None

    def as_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "pool_name": self.pool_name,
            "lanes": list(self.lanes),
            "runtime_role": self.runtime_role,
            "runtime_version": self.runtime_version,
            "draining": self.draining,
            "active_jobs": self.active_jobs,
            "max_concurrency": self.max_concurrency,
            "available_concurrency": self.available_concurrency,
            "queue_pressure": dict(self.queue_pressure),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "is_stale": self.is_stale,
            "age_seconds": self.age_seconds,
        }


def _row_to_view(row: Mapping, *, now: datetime, stale_after_seconds: float) -> InstanceHealthView:
    heartbeat_at = _dt_from_db(row["heartbeat_at"])
    age = None
    is_stale = True
    if heartbeat_at is not None:
        age = max(0.0, (now - heartbeat_at).total_seconds())
        is_stale = age > stale_after_seconds
    try:
        lanes = tuple(json.loads(row["lanes_json"] or "[]"))
    except (TypeError, ValueError):
        lanes = ()
    try:
        pressure = json.loads(row["queue_pressure_json"] or "{}")
    except (TypeError, ValueError):
        pressure = {}
    return InstanceHealthView(
        instance_id=str(row["instance_id"]),
        pool_name=str(row["pool_name"] or ""),
        lanes=lanes,
        runtime_role=str(row["runtime_role"] or ""),
        runtime_version=str(row["runtime_version"] or ""),
        draining=bool(row["draining"]),
        active_jobs=int(row["active_jobs"] or 0),
        max_concurrency=int(row["max_concurrency"] or 0),
        available_concurrency=int(row["available_concurrency"] or 0),
        queue_pressure=pressure,
        started_at=_dt_from_db(row["started_at"]),
        heartbeat_at=heartbeat_at,
        is_stale=is_stale,
        age_seconds=age,
    )


class FleetRegistryStore:
    """Abstract shared fleet-health backend."""

    def upsert_heartbeat(self, **kwargs) -> None:
        raise NotImplementedError

    def deregister(self, instance_id: str) -> None:
        raise NotImplementedError

    def list_instances(self, *, now: datetime | None = None) -> tuple[dict, ...]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class InMemoryFleetRegistryStore(FleetRegistryStore):
    """Dev/test store -- NOT authoritative for multi-process (Scale 3.29)."""

    def __init__(self):
        self._lock = threading.RLock()
        self._rows: dict[str, dict] = {}

    def upsert_heartbeat(self, **kwargs) -> None:
        now = kwargs.pop("now", None) or utc_now()
        instance_id = str(kwargs["instance_id"])
        with self._lock:
            existing = self._rows.get(instance_id, {})
            started_at = existing.get("started_at") or now
            self._rows[instance_id] = {
                "instance_id": instance_id,
                "pool_name": kwargs.get("pool_name", ""),
                "lanes_json": json.dumps(list(kwargs.get("lanes") or [])),
                "runtime_role": kwargs.get("runtime_role", ""),
                "runtime_version": kwargs.get("runtime_version", ""),
                "draining": bool(kwargs.get("draining", False)),
                "active_jobs": int(kwargs.get("active_jobs", 0)),
                "max_concurrency": int(kwargs.get("max_concurrency", 0)),
                "available_concurrency": int(kwargs.get("available_concurrency", 0)),
                "queue_pressure_json": json.dumps(dict(kwargs.get("queue_pressure") or {})),
                "started_at": started_at,
                "heartbeat_at": now,
            }

    def deregister(self, instance_id: str) -> None:
        with self._lock:
            self._rows.pop(str(instance_id), None)

    def list_instances(self, *, now: datetime | None = None) -> tuple[dict, ...]:
        with self._lock:
            rows = []
            for row in self._rows.values():
                r = dict(row)
                r["started_at"] = _dt_to_db(r["started_at"])
                r["heartbeat_at"] = _dt_to_db(r["heartbeat_at"])
                rows.append(r)
            return tuple(rows)


DDL_FLEET = """
CREATE TABLE IF NOT EXISTS runtime_instances (
    instance_id TEXT PRIMARY KEY,
    pool_name TEXT NOT NULL DEFAULT '',
    lanes_json TEXT NOT NULL DEFAULT '[]',
    runtime_role TEXT NOT NULL DEFAULT '',
    runtime_version TEXT NOT NULL DEFAULT '',
    draining INTEGER NOT NULL DEFAULT 0,
    active_jobs INTEGER NOT NULL DEFAULT 0,
    max_concurrency INTEGER NOT NULL DEFAULT 0,
    available_concurrency INTEGER NOT NULL DEFAULT 0,
    queue_pressure_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runtime_instances_heartbeat
    ON runtime_instances(heartbeat_at);
"""


class SqliteFleetRegistryStore(FleetRegistryStore):
    """Authoritative shared registry using WAL SQLite (multi-instance safe)."""

    def __init__(self, path: str):
        self.path = str(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path, check_same_thread=False, isolation_level=None, timeout=30.0
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(DDL_FLEET)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def upsert_heartbeat(self, **kwargs) -> None:
        now = kwargs.pop("now", None) or utc_now()
        instance_id = str(kwargs["instance_id"])
        now_s = _dt_to_db(now)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT started_at FROM runtime_instances WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            started_s = existing["started_at"] if existing is not None else now_s
            conn.execute(
                """
                INSERT INTO runtime_instances(
                    instance_id, pool_name, lanes_json, runtime_role, runtime_version,
                    draining, active_jobs, max_concurrency, available_concurrency,
                    queue_pressure_json, started_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    pool_name = excluded.pool_name,
                    lanes_json = excluded.lanes_json,
                    runtime_role = excluded.runtime_role,
                    runtime_version = excluded.runtime_version,
                    draining = excluded.draining,
                    active_jobs = excluded.active_jobs,
                    max_concurrency = excluded.max_concurrency,
                    available_concurrency = excluded.available_concurrency,
                    queue_pressure_json = excluded.queue_pressure_json,
                    heartbeat_at = excluded.heartbeat_at
                """,
                (
                    instance_id,
                    str(kwargs.get("pool_name", "")),
                    json.dumps(list(kwargs.get("lanes") or [])),
                    str(kwargs.get("runtime_role", "")),
                    str(kwargs.get("runtime_version", "")),
                    1 if kwargs.get("draining") else 0,
                    int(kwargs.get("active_jobs", 0)),
                    int(kwargs.get("max_concurrency", 0)),
                    int(kwargs.get("available_concurrency", 0)),
                    json.dumps(dict(kwargs.get("queue_pressure") or {})),
                    started_s,
                    now_s,
                ),
            )

    def deregister(self, instance_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM runtime_instances WHERE instance_id = ?",
                (str(instance_id),),
            )

    def list_instances(self, *, now: datetime | None = None) -> tuple[dict, ...]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM runtime_instances").fetchall()
            return tuple(dict(row) for row in rows)

    def close(self) -> None:
        with self._lock:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None


class FleetRegistry:
    """Facade used by the workflow runtime and observability/admin surfaces.

    Read paths always recompute staleness against ``now`` -- the shared
    store is never trusted to self-report liveness.
    """

    def __init__(
        self,
        store: FleetRegistryStore | None = None,
        config: FleetRegistryConfig | None = None,
    ):
        self.config = config or FleetRegistryConfig()
        self.store = store or InMemoryFleetRegistryStore()

    def heartbeat(
        self,
        *,
        instance_id: str,
        pool_name: str = "",
        lanes: Sequence[str] = (),
        runtime_role: str = "",
        runtime_version: str = "",
        draining: bool = False,
        active_jobs: int = 0,
        max_concurrency: int = 0,
        available_concurrency: int = 0,
        queue_pressure: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        if not self.config.enabled:
            return
        self.store.upsert_heartbeat(
            instance_id=instance_id,
            pool_name=pool_name,
            lanes=list(lanes),
            runtime_role=runtime_role,
            runtime_version=runtime_version,
            draining=draining,
            active_jobs=active_jobs,
            max_concurrency=max_concurrency,
            available_concurrency=available_concurrency,
            queue_pressure=queue_pressure or {},
            now=now,
        )

    def deregister(self, instance_id: str) -> None:
        self.store.deregister(instance_id)

    def snapshot(self, *, now: datetime | None = None) -> tuple[InstanceHealthView, ...]:
        stamp = now or utc_now()
        rows = self.store.list_instances(now=stamp)
        return tuple(
            _row_to_view(row, now=stamp, stale_after_seconds=self.config.stale_after_seconds)
            for row in rows
        )

    def active_instance_count(self, *, now: datetime | None = None) -> int:
        return sum(1 for view in self.snapshot(now=now) if not view.is_stale)

    def fleet_summary(self, *, now: datetime | None = None) -> dict:
        """Bounded-cardinality fleet-wide summary (Scale 3.29/3.34)."""

        views = self.snapshot(now=now)
        active = [v for v in views if not v.is_stale]
        return {
            "instance_count": len(views),
            "active_instance_count": len(active),
            "stale_instance_count": len(views) - len(active),
            "draining_instance_count": sum(1 for v in active if v.draining),
            "total_active_jobs": sum(v.active_jobs for v in active),
            "total_max_concurrency": sum(v.max_concurrency for v in active),
            "total_available_concurrency": sum(v.available_concurrency for v in active),
            "instances": [v.as_dict() for v in views],
        }
