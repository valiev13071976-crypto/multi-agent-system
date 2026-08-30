"""Shared SQLite routing health store (Scale 3.28+).

Uses the same DB path pattern as ProviderGovernor / side_effects (SIDE_EFFECT_DB_PATH
or ROUTING_HEALTH_DB_PATH). Fail-safe: if the store cannot open or write, callers
must not claim multi_worker_shared_routing_health_ready.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Mapping

from agents.routing_health import (
    DEFAULT_HEALTH_COOLDOWN_SECONDS,
    DEFAULT_HEALTH_FAILURE_THRESHOLD,
    DEFAULT_HEALTH_WINDOW_SECONDS,
    HEALTH_COOLDOWN,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    ProviderHealthSnapshot,
    REASON_COOLDOWN_ACTIVE,
    REASON_INSUFFICIENT_SAMPLES,
    REASON_REPEATED_FAILURES,
    RoutingHealthPolicy,
    utc_now,
)
from agents.routing_state_scope import STATE_SCOPE_SHARED

DDL = """
CREATE TABLE IF NOT EXISTS routing_provider_health (
    state_key TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    failure_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    window_started_at TEXT,
    updated_at TEXT NOT NULL
);
"""


def _dt_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_db(raw) -> datetime | None:
    if raw is None or raw == "":
        return None
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def resolve_routing_health_db_path(env: Mapping | None = None) -> str | None:
    source = env if env is not None else os.environ
    for key in ("ROUTING_HEALTH_DB_PATH", "PROVIDER_GOVERNOR_DB_PATH", "SIDE_EFFECT_DB_PATH"):
        raw = source.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


@dataclass
class SqliteRoutingHealthStore:
    """Shared upsert store for provider health counters."""

    path: str
    policy: RoutingHealthPolicy | None = None
    _available: bool = True

    def __post_init__(self):
        self.policy = self.policy or RoutingHealthPolicy()
        self._lock = threading.RLock()
        self._local = threading.local()
        try:
            self._init_schema()
            self._available = True
        except Exception:
            self._available = False

    @property
    def state_scope(self) -> str:
        return STATE_SCOPE_SHARED

    @property
    def shared_backing(self) -> bool:
        return bool(self._available)

    @property
    def available(self) -> bool:
        return bool(self._available)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path,
                check_same_thread=False,
                isolation_level=None,
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(DDL)

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

    def _key(self, provider_id: str, model_id: str = "") -> str:
        return f"{provider_id}:{model_id or '*'}"

    def _ensure(self, conn: sqlite3.Connection, provider_id: str, model_id: str, now_s: str):
        key = self._key(provider_id, model_id)
        row = conn.execute(
            "SELECT * FROM routing_provider_health WHERE state_key = ?", (key,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO routing_provider_health(
                    state_key, provider_id, model_id, failure_count, success_count,
                    sample_count, cooldown_until, window_started_at, updated_at
                ) VALUES (?, ?, ?, 0, 0, 0, NULL, ?, ?)
                """,
                (key, provider_id, model_id or "", now_s, now_s),
            )
            row = conn.execute(
                "SELECT * FROM routing_provider_health WHERE state_key = ?", (key,)
            ).fetchone()
        return row

    def record_failure(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        error_class: str = "",
        now: datetime | None = None,
    ) -> ProviderHealthSnapshot:
        del error_class  # stored counters only; class used by in-memory tracker
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        now_s = _dt_to_db(stamp)
        try:
            with self._tx() as conn:
                row = self._ensure(conn, provider_id, model_id, now_s or "")
                failures = int(row["failure_count"]) + 1
                samples = int(row["sample_count"]) + 1
                cooldown = row["cooldown_until"]
                if failures >= self.policy.failure_threshold:
                    until = stamp + timedelta(seconds=self.policy.cooldown_seconds)
                    cooldown = _dt_to_db(until)
                conn.execute(
                    """
                    UPDATE routing_provider_health
                    SET failure_count = ?, sample_count = ?, cooldown_until = ?,
                        updated_at = ?
                    WHERE state_key = ?
                    """,
                    (
                        failures,
                        samples,
                        cooldown,
                        now_s,
                        self._key(provider_id, model_id),
                    ),
                )
            self._available = True
        except sqlite3.Error:
            self._available = False
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_UNKNOWN,
                reason_code=REASON_INSUFFICIENT_SAMPLES,
                window_seconds=self.policy.window_seconds,
            )
        return self.snapshot(provider_id, model_id, now=stamp)

    def record_success(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now: datetime | None = None,
    ) -> ProviderHealthSnapshot:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        now_s = _dt_to_db(stamp)
        try:
            with self._tx() as conn:
                row = self._ensure(conn, provider_id, model_id, now_s or "")
                successes = int(row["success_count"]) + 1
                samples = int(row["sample_count"]) + 1
                conn.execute(
                    """
                    UPDATE routing_provider_health
                    SET success_count = ?, failure_count = 0, sample_count = ?,
                        cooldown_until = NULL, updated_at = ?
                    WHERE state_key = ?
                    """,
                    (successes, samples, now_s, self._key(provider_id, model_id)),
                )
            self._available = True
        except sqlite3.Error:
            self._available = False
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_UNKNOWN,
                reason_code=REASON_INSUFFICIENT_SAMPLES,
                window_seconds=self.policy.window_seconds,
            )
        return self.snapshot(provider_id, model_id, now=stamp)

    def snapshot(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now: datetime | None = None,
    ) -> ProviderHealthSnapshot:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        try:
            with self._lock:
                conn = self._connect()
                row = conn.execute(
                    "SELECT * FROM routing_provider_health WHERE state_key = ?",
                    (self._key(provider_id, model_id),),
                ).fetchone()
            self._available = True
        except sqlite3.Error:
            self._available = False
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_UNKNOWN,
                reason_code=REASON_INSUFFICIENT_SAMPLES,
                window_seconds=DEFAULT_HEALTH_WINDOW_SECONDS,
            )
        if row is None:
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_UNKNOWN,
                reason_code=REASON_INSUFFICIENT_SAMPLES,
                window_seconds=self.policy.window_seconds,
                sample_count=0,
            )
        until = _dt_from_db(row["cooldown_until"])
        failures = int(row["failure_count"])
        successes = int(row["success_count"])
        samples = int(row["sample_count"])
        if until is not None and until > stamp:
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_COOLDOWN,
                recent_failure_count=failures,
                recent_success_count=successes,
                cooldown_until=until,
                reason_code=REASON_COOLDOWN_ACTIVE,
                window_seconds=self.policy.window_seconds,
                sample_count=samples,
            )
        if samples == 0:
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_UNKNOWN,
                reason_code=REASON_INSUFFICIENT_SAMPLES,
                window_seconds=self.policy.window_seconds,
                sample_count=0,
            )
        if failures > 0:
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_DEGRADED,
                recent_failure_count=failures,
                recent_success_count=successes,
                reason_code=REASON_REPEATED_FAILURES if failures > 1 else None,
                window_seconds=self.policy.window_seconds,
                sample_count=samples,
            )
        return ProviderHealthSnapshot(
            provider_id=provider_id,
            model_id=model_id,
            state=HEALTH_HEALTHY,
            recent_failure_count=0,
            recent_success_count=successes,
            window_seconds=self.policy.window_seconds,
            sample_count=samples,
        )

    def is_auto_eligible(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now: datetime | None = None,
    ) -> bool:
        if not self.policy.enabled:
            return True
        return self.snapshot(provider_id, model_id, now=now).auto_eligible

    def get_snapshot(self) -> dict[str, dict]:
        """All provider rows as a machine-readable map."""

        out: dict[str, dict] = {}
        try:
            with self._lock:
                conn = self._connect()
                rows = conn.execute("SELECT * FROM routing_provider_health").fetchall()
            self._available = True
        except sqlite3.Error:
            self._available = False
            return out
        for row in rows:
            key = str(row["state_key"])
            snap = self.snapshot(row["provider_id"], row["model_id"])
            out[key] = {
                "provider_id": snap.provider_id,
                "model_id": snap.model_id,
                "state": snap.state,
                "recent_failure_count": snap.recent_failure_count,
                "recent_success_count": snap.recent_success_count,
                "sample_count": snap.sample_count,
                "cooldown_until": (
                    snap.cooldown_until.isoformat() if snap.cooldown_until else None
                ),
                "reason_code": snap.reason_code,
            }
        return out


def open_routing_health_store(
    env: Mapping | None = None,
    *,
    path: str | None = None,
    policy: RoutingHealthPolicy | None = None,
) -> SqliteRoutingHealthStore | None:
    """Open shared store or return None (fail-safe — do not claim shared ready)."""

    db = path or resolve_routing_health_db_path(env)
    if not db:
        return None
    try:
        store = SqliteRoutingHealthStore(db, policy=policy)
        if not store.available:
            return None
        return store
    except Exception:
        return None


# Re-export defaults used by readiness docs.
__all__ = [
    "SqliteRoutingHealthStore",
    "open_routing_health_store",
    "resolve_routing_health_db_path",
    "DEFAULT_HEALTH_COOLDOWN_SECONDS",
    "DEFAULT_HEALTH_FAILURE_THRESHOLD",
    "DEFAULT_HEALTH_WINDOW_SECONDS",
]
