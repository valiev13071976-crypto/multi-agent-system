"""Shared provider capacity governor + circuit breaker (Phase 3 Block 2).

Separate from ProviderHealthTracker (behavior) — this governs quota/capacity.
SQLite-backed so API/worker replicas share authoritative state.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Mapping

from side_effects.errors import SideEffectPersistenceUnavailableError

STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"

# Failures that must NOT trip the provider breaker.
NON_QUALIFYING_ERRORS = frozenset(
    {
        "validation_error",
        "capability_denied",
        "capability_mismatch",
        "tool_argument_invalid",
        "tool_permission_denied",
        "hitl_denied",
        "integration_access_denied",
        "scope_insufficient",
        "idempotency_conflict",
        "user_error",
        "bad_request",
    }
)

LANE_INTERACTIVE = "interactive"
LANE_BACKGROUND = "background"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


@dataclass(frozen=True)
class GovernorLimits:
    max_concurrency: int = 8
    interactive_reserved: int = 2
    background_may_borrow: bool = True
    max_rpm: int | None = 120
    max_qps: int | None = 20
    max_tpm: int | None = None
    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    half_open_probe_limit: int = 1
    slot_ttl_seconds: float = 120.0
    enabled: bool = True

    @classmethod
    def from_env(cls, env: Mapping | None = None) -> "GovernorLimits":
        source = env if env is not None else os.environ

        def _int(name: str, default: int | None) -> int | None:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                value = int(str(raw).strip())
            except ValueError:
                return default
            if value <= 0:
                return None
            return value

        def _float(name: str, default: float) -> float:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                return max(0.0, float(str(raw).strip()))
            except ValueError:
                return default

        def _bool(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            max_concurrency=_int("PROVIDER_MAX_CONCURRENCY", 8) or 8,
            interactive_reserved=_int("PROVIDER_INTERACTIVE_RESERVED", 2) or 0,
            background_may_borrow=_bool("PROVIDER_BACKGROUND_MAY_BORROW", True),
            max_rpm=_int("PROVIDER_MAX_RPM", 120),
            max_qps=_int("PROVIDER_MAX_QPS", 20),
            max_tpm=_int("PROVIDER_MAX_TPM", None),
            failure_threshold=_int("PROVIDER_BREAKER_FAILURE_THRESHOLD", 5) or 5,
            cooldown_seconds=_float("PROVIDER_BREAKER_COOLDOWN_SECONDS", 30.0),
            half_open_probe_limit=_int("PROVIDER_BREAKER_HALF_OPEN_PROBES", 1) or 1,
            slot_ttl_seconds=_float("PROVIDER_SLOT_TTL_SECONDS", 120.0),
            enabled=_bool("PROVIDER_GOVERNOR_ENABLED", True),
        )


class ProviderCapacityUnavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ProviderGovernorStore:
    """Abstract shared capacity backend (SQLite now; swappable later)."""

    def acquire(self, *args, **kwargs):
        raise NotImplementedError

    def release(self, slot_id: str) -> None:
        raise NotImplementedError

    def record_429(self, *args, **kwargs) -> None:
        raise NotImplementedError

    def record_success(self, *args, **kwargs) -> None:
        raise NotImplementedError

    def record_failure(self, *args, **kwargs) -> None:
        raise NotImplementedError

    def record_tokens(
        self, provider_id: str, model_id: str = "", tokens: int | None = None, **kwargs
    ) -> None:
        raise NotImplementedError

    def breaker_state(self, provider_id: str, model_id: str = "") -> str:
        raise NotImplementedError

    def is_available(
        self, provider_id: str, model_id: str = "", *, lane: str = LANE_BACKGROUND
    ) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        return None


def _blank_governor_state(stamp: datetime) -> dict:
    return {
        "breaker": STATE_CLOSED,
        "failures": 0,
        "opened_at": None,
        "half_open_probes": 0,
        "throttle_until": None,
        "rpm_window_start": stamp,
        "rpm_count": 0,
        "qps_window_start": stamp,
        "qps_count": 0,
        "tpm_window_start": stamp,
        "tpm_count": 0,
    }


class InMemoryProviderGovernorStore(ProviderGovernorStore):
    """Dev/test store — not authoritative for multi-process."""

    def __init__(self, limits: GovernorLimits | None = None):
        self.limits = limits or GovernorLimits()
        self._lock = threading.RLock()
        self._slots: dict[str, dict] = {}
        self._state: dict[str, dict] = {}

    def _key(self, provider_id: str, model_id: str = "") -> str:
        return f"{provider_id}:{model_id or '*'}"

    def _ensure_fields(self, st: dict, stamp: datetime) -> None:
        st.setdefault("qps_window_start", stamp)
        st.setdefault("qps_count", 0)
        st.setdefault("tpm_window_start", stamp)
        st.setdefault("tpm_count", 0)

    def _purge(self, now: datetime) -> None:
        expired = [
            sid
            for sid, row in self._slots.items()
            if row["expires_at"] <= now
        ]
        for sid in expired:
            self._slots.pop(sid, None)

    def acquire(
        self,
        *,
        provider_id: str,
        model_id: str = "",
        lane: str = LANE_BACKGROUND,
        worker_id: str = "",
        now: datetime | None = None,
    ) -> str:
        if not self.limits.enabled:
            return f"disabled-{uuid.uuid4().hex}"
        stamp = now or utc_now()
        with self._lock:
            self._purge(stamp)
            key = self._key(provider_id, model_id)
            st = self._state.setdefault(key, _blank_governor_state(stamp))
            self._ensure_fields(st, stamp)
            if st["throttle_until"] and st["throttle_until"] > stamp:
                raise ProviderCapacityUnavailable("provider_429_throttle")
            self._maybe_transition(st, stamp)
            if st["breaker"] == STATE_OPEN:
                raise ProviderCapacityUnavailable("provider_circuit_open")
            if st["breaker"] == STATE_HALF_OPEN:
                if st["half_open_probes"] >= self.limits.half_open_probe_limit:
                    raise ProviderCapacityUnavailable("provider_circuit_open")
                st["half_open_probes"] += 1
            slots = [
                s
                for s in self._slots.values()
                if s["provider_id"] == provider_id
                and s["model_id"] == (model_id or "")
            ]
            if len(slots) >= self.limits.max_concurrency:
                raise ProviderCapacityUnavailable("provider_concurrency_limit")
            interactive = sum(1 for s in slots if s["lane"] == LANE_INTERACTIVE)
            background = len(slots) - interactive
            max_bg = max(
                0, self.limits.max_concurrency - self.limits.interactive_reserved
            )
            if lane != LANE_INTERACTIVE and background >= max_bg:
                if not (
                    self.limits.background_may_borrow
                    and interactive == 0
                    and len(slots) < self.limits.max_concurrency
                ):
                    raise ProviderCapacityUnavailable("provider_interactive_reserved")
            if self.limits.max_rpm is not None:
                window = st["rpm_window_start"]
                if (stamp - window).total_seconds() >= 60:
                    st["rpm_window_start"] = stamp
                    st["rpm_count"] = 0
                if st["rpm_count"] >= self.limits.max_rpm:
                    raise ProviderCapacityUnavailable("provider_rpm_limit")
                st["rpm_count"] += 1
            if self.limits.max_qps is not None:
                qps_window = st["qps_window_start"]
                if (stamp - qps_window).total_seconds() >= 1:
                    st["qps_window_start"] = stamp
                    st["qps_count"] = 0
                if st["qps_count"] >= self.limits.max_qps:
                    raise ProviderCapacityUnavailable("provider_qps_limit")
                st["qps_count"] = int(st["qps_count"]) + 1
            if self.limits.max_tpm is not None:
                tpm_window = st["tpm_window_start"]
                if (stamp - tpm_window).total_seconds() >= 60:
                    st["tpm_window_start"] = stamp
                    st["tpm_count"] = 0
                if int(st["tpm_count"] or 0) >= self.limits.max_tpm:
                    raise ProviderCapacityUnavailable("provider_tpm_limit")
            slot_id = str(uuid.uuid4())
            self._slots[slot_id] = {
                "provider_id": provider_id,
                "model_id": model_id or "",
                "lane": lane if lane == LANE_INTERACTIVE else LANE_BACKGROUND,
                "worker_id": worker_id,
                "expires_at": stamp + timedelta(seconds=self.limits.slot_ttl_seconds),
            }
            return slot_id

    def release(self, slot_id: str) -> None:
        with self._lock:
            self._slots.pop(slot_id, None)

    def record_tokens(
        self,
        provider_id: str,
        model_id: str = "",
        tokens: int | None = None,
        *,
        now: datetime | None = None,
        **kwargs,
    ) -> None:
        if tokens is None:
            return
        try:
            amount = int(tokens)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        stamp = now or utc_now()
        with self._lock:
            st = self._state.setdefault(
                self._key(provider_id, model_id), _blank_governor_state(stamp)
            )
            self._ensure_fields(st, stamp)
            tpm_window = st["tpm_window_start"]
            if (stamp - tpm_window).total_seconds() >= 60:
                st["tpm_window_start"] = stamp
                st["tpm_count"] = 0
            st["tpm_count"] = int(st["tpm_count"] or 0) + amount

    def record_429(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        retry_after_seconds: float | None = None,
        now: datetime | None = None,
    ) -> None:
        stamp = now or utc_now()
        delay = float(retry_after_seconds) if retry_after_seconds is not None else 30.0
        with self._lock:
            st = self._state.setdefault(
                self._key(provider_id, model_id), _blank_governor_state(stamp)
            )
            self._ensure_fields(st, stamp)
            st["throttle_until"] = stamp + timedelta(seconds=max(1.0, delay))
            st["failures"] = int(st["failures"]) + 1
            if st["failures"] >= self.limits.failure_threshold:
                st["breaker"] = STATE_OPEN
                st["opened_at"] = stamp

    def record_success(self, provider_id: str, model_id: str = "") -> None:
        with self._lock:
            stamp = utc_now()
            st = self._state.setdefault(
                self._key(provider_id, model_id), _blank_governor_state(stamp)
            )
            self._ensure_fields(st, stamp)
            st["breaker"] = STATE_CLOSED
            st["failures"] = 0
            st["opened_at"] = None
            st["half_open_probes"] = 0

    def record_failure(
        self, provider_id: str, model_id: str = "", *, error_code: str = "", now: datetime | None = None
    ) -> None:
        if error_code in NON_QUALIFYING_ERRORS:
            return
        stamp = now or utc_now()
        with self._lock:
            st = self._state.setdefault(
                self._key(provider_id, model_id), _blank_governor_state(stamp)
            )
            self._ensure_fields(st, stamp)
            st["failures"] = int(st["failures"]) + 1
            if st["breaker"] == STATE_HALF_OPEN:
                st["breaker"] = STATE_OPEN
                st["opened_at"] = stamp
                st["half_open_probes"] = 0
                return
            if st["failures"] >= self.limits.failure_threshold:
                st["breaker"] = STATE_OPEN
                st["opened_at"] = stamp

    def breaker_state(self, provider_id: str, model_id: str = "", *, now: datetime | None = None) -> str:
        with self._lock:
            st = self._state.get(self._key(provider_id, model_id))
            if st is None:
                return STATE_CLOSED
            self._maybe_transition(st, now or utc_now())
            return st["breaker"]

    def is_available(
        self, provider_id: str, model_id: str = "", *, lane: str = LANE_BACKGROUND
    ) -> bool:
        try:
            slot = self.acquire(
                provider_id=provider_id, model_id=model_id, lane=lane, worker_id="probe"
            )
            self.release(slot)
            return True
        except ProviderCapacityUnavailable:
            return False

    def _maybe_transition(self, st: dict, now: datetime) -> None:
        if st["breaker"] != STATE_OPEN or st["opened_at"] is None:
            return
        opened = st["opened_at"]
        if (now - opened).total_seconds() >= self.limits.cooldown_seconds:
            st["breaker"] = STATE_HALF_OPEN
            st["half_open_probes"] = 0


DDL_GOVERNOR = """
CREATE TABLE IF NOT EXISTS provider_governor_slots (
    slot_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    lane TEXT NOT NULL DEFAULT 'background',
    worker_id TEXT,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gov_slots_provider
    ON provider_governor_slots(provider_id, model_id, expires_at);
CREATE TABLE IF NOT EXISTS provider_governor_state (
    state_key TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    breaker_state TEXT NOT NULL DEFAULT 'CLOSED',
    failure_count INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT,
    half_open_probes INTEGER NOT NULL DEFAULT 0,
    throttle_until TEXT,
    rpm_window_start TEXT,
    rpm_count INTEGER NOT NULL DEFAULT 0,
    qps_window_start TEXT,
    qps_count INTEGER NOT NULL DEFAULT 0,
    tpm_window_start TEXT,
    tpm_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


class SqliteProviderGovernorStore(ProviderGovernorStore):
    """Authoritative shared governor using WAL SQLite."""

    def __init__(self, path: str, limits: GovernorLimits | None = None):
        self.path = str(path)
        self.limits = limits or GovernorLimits()
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init_schema()

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
            conn.executescript(DDL_GOVERNOR)
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(provider_governor_state)").fetchall()
            }
            alters = [
                ("qps_window_start", "ALTER TABLE provider_governor_state ADD COLUMN qps_window_start TEXT"),
                ("qps_count", "ALTER TABLE provider_governor_state ADD COLUMN qps_count INTEGER NOT NULL DEFAULT 0"),
                ("tpm_window_start", "ALTER TABLE provider_governor_state ADD COLUMN tpm_window_start TEXT"),
                ("tpm_count", "ALTER TABLE provider_governor_state ADD COLUMN tpm_count INTEGER NOT NULL DEFAULT 0"),
            ]
            for name, ddl in alters:
                if name not in cols:
                    conn.execute(ddl)

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

    def _purge(self, conn: sqlite3.Connection, now_s: str) -> None:
        conn.execute(
            "DELETE FROM provider_governor_slots WHERE expires_at <= ?", (now_s,)
        )

    def _ensure_state(self, conn: sqlite3.Connection, provider_id: str, model_id: str, now_s: str):
        key = self._key(provider_id, model_id)
        row = conn.execute(
            "SELECT * FROM provider_governor_state WHERE state_key = ?", (key,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO provider_governor_state(
                    state_key, provider_id, model_id, breaker_state, failure_count,
                    half_open_probes, rpm_window_start, rpm_count, updated_at
                ) VALUES (?, ?, ?, 'CLOSED', 0, 0, ?, 0, ?)
                """,
                (key, provider_id, model_id or "", now_s, now_s),
            )
            return conn.execute(
                "SELECT * FROM provider_governor_state WHERE state_key = ?", (key,)
            ).fetchone()
        return row

    def _maybe_transition_row(self, conn, row, now: datetime, now_s: str):
        if row["breaker_state"] != STATE_OPEN or not row["opened_at"]:
            return row
        opened = _dt_from_db(row["opened_at"])
        if opened is None:
            return row
        if (now - opened).total_seconds() >= self.limits.cooldown_seconds:
            conn.execute(
                """
                UPDATE provider_governor_state SET
                    breaker_state = ?, half_open_probes = 0, updated_at = ?
                WHERE state_key = ?
                """,
                (STATE_HALF_OPEN, now_s, row["state_key"]),
            )
            return conn.execute(
                "SELECT * FROM provider_governor_state WHERE state_key = ?",
                (row["state_key"],),
            ).fetchone()
        return row

    def acquire(
        self,
        *,
        provider_id: str,
        model_id: str = "",
        lane: str = LANE_BACKGROUND,
        worker_id: str = "",
        now: datetime | None = None,
    ) -> str:
        if not self.limits.enabled:
            return f"disabled-{uuid.uuid4().hex}"
        stamp = now or utc_now()
        now_s = _dt_to_db(stamp)
        expires_s = _dt_to_db(
            stamp + timedelta(seconds=float(self.limits.slot_ttl_seconds))
        )
        lane_norm = LANE_INTERACTIVE if lane == LANE_INTERACTIVE else LANE_BACKGROUND
        try:
            with self._tx() as conn:
                self._purge(conn, now_s)
                row = self._ensure_state(conn, provider_id, model_id, now_s)
                row = self._maybe_transition_row(conn, row, stamp, now_s)
                throttle = _dt_from_db(row["throttle_until"])
                if throttle is not None and throttle > stamp:
                    raise ProviderCapacityUnavailable("provider_429_throttle")
                if row["breaker_state"] == STATE_OPEN:
                    raise ProviderCapacityUnavailable("provider_circuit_open")
                if row["breaker_state"] == STATE_HALF_OPEN:
                    if int(row["half_open_probes"]) >= self.limits.half_open_probe_limit:
                        raise ProviderCapacityUnavailable("provider_circuit_open")
                    conn.execute(
                        """
                        UPDATE provider_governor_state SET
                            half_open_probes = half_open_probes + 1, updated_at = ?
                        WHERE state_key = ?
                        """,
                        (now_s, row["state_key"]),
                    )
                slots = conn.execute(
                    """
                    SELECT lane FROM provider_governor_slots
                    WHERE provider_id = ? AND model_id = ? AND expires_at > ?
                    """,
                    (provider_id, model_id or "", now_s),
                ).fetchall()
                total = len(slots)
                if total >= self.limits.max_concurrency:
                    raise ProviderCapacityUnavailable("provider_concurrency_limit")
                interactive = sum(1 for s in slots if s["lane"] == LANE_INTERACTIVE)
                background = total - interactive
                max_bg = max(
                    0, self.limits.max_concurrency - self.limits.interactive_reserved
                )
                if lane_norm != LANE_INTERACTIVE and background >= max_bg:
                    if not (
                        self.limits.background_may_borrow
                        and interactive == 0
                        and total < self.limits.max_concurrency
                    ):
                        raise ProviderCapacityUnavailable(
                            "provider_interactive_reserved"
                        )
                if self.limits.max_rpm is not None:
                    window_start = _dt_from_db(row["rpm_window_start"]) or stamp
                    rpm_count = int(row["rpm_count"] or 0)
                    if (stamp - window_start).total_seconds() >= 60:
                        window_start = stamp
                        rpm_count = 0
                    if rpm_count >= self.limits.max_rpm:
                        raise ProviderCapacityUnavailable("provider_rpm_limit")
                    conn.execute(
                        """
                        UPDATE provider_governor_state SET
                            rpm_window_start = ?, rpm_count = ?, updated_at = ?
                        WHERE state_key = ?
                        """,
                        (
                            _dt_to_db(window_start),
                            rpm_count + 1,
                            now_s,
                            row["state_key"],
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM provider_governor_state WHERE state_key = ?",
                        (row["state_key"],),
                    ).fetchone()
                if self.limits.max_qps is not None:
                    qps_start = _dt_from_db(row["qps_window_start"]) or stamp
                    qps_count = int(row["qps_count"] or 0)
                    if (stamp - qps_start).total_seconds() >= 1:
                        qps_start = stamp
                        qps_count = 0
                    if qps_count >= self.limits.max_qps:
                        raise ProviderCapacityUnavailable("provider_qps_limit")
                    conn.execute(
                        """
                        UPDATE provider_governor_state SET
                            qps_window_start = ?, qps_count = ?, updated_at = ?
                        WHERE state_key = ?
                        """,
                        (
                            _dt_to_db(qps_start),
                            qps_count + 1,
                            now_s,
                            row["state_key"],
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM provider_governor_state WHERE state_key = ?",
                        (row["state_key"],),
                    ).fetchone()
                if self.limits.max_tpm is not None:
                    tpm_start = _dt_from_db(row["tpm_window_start"]) or stamp
                    tpm_count = int(row["tpm_count"] or 0)
                    if (stamp - tpm_start).total_seconds() >= 60:
                        tpm_start = stamp
                        tpm_count = 0
                        conn.execute(
                            """
                            UPDATE provider_governor_state SET
                                tpm_window_start = ?, tpm_count = 0, updated_at = ?
                            WHERE state_key = ?
                            """,
                            (_dt_to_db(tpm_start), now_s, row["state_key"]),
                        )
                    elif tpm_count >= self.limits.max_tpm:
                        raise ProviderCapacityUnavailable("provider_tpm_limit")
                slot_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO provider_governor_slots(
                        slot_id, provider_id, model_id, lane, worker_id,
                        acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slot_id,
                        provider_id,
                        model_id or "",
                        lane_norm,
                        worker_id,
                        now_s,
                        expires_s,
                    ),
                )
                return slot_id
        except ProviderCapacityUnavailable:
            raise
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "provider_governor_unavailable"
            ) from exc

    def release(self, slot_id: str) -> None:
        if not slot_id or str(slot_id).startswith("disabled-"):
            return
        try:
            with self._tx() as conn:
                conn.execute(
                    "DELETE FROM provider_governor_slots WHERE slot_id = ?", (slot_id,)
                )
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "provider_governor_unavailable"
            ) from exc

    def record_tokens(
        self,
        provider_id: str,
        model_id: str = "",
        tokens: int | None = None,
        *,
        now: datetime | None = None,
        **kwargs,
    ) -> None:
        if tokens is None:
            return
        try:
            amount = int(tokens)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        stamp = now or utc_now()
        now_s = _dt_to_db(stamp)
        try:
            with self._tx() as conn:
                row = self._ensure_state(conn, provider_id, model_id, now_s)
                tpm_start = _dt_from_db(row["tpm_window_start"]) or stamp
                tpm_count = int(row["tpm_count"] or 0)
                if (stamp - tpm_start).total_seconds() >= 60:
                    tpm_start = stamp
                    tpm_count = 0
                conn.execute(
                    """
                    UPDATE provider_governor_state SET
                        tpm_window_start = ?, tpm_count = ?, updated_at = ?
                    WHERE state_key = ?
                    """,
                    (
                        _dt_to_db(tpm_start),
                        tpm_count + amount,
                        now_s,
                        row["state_key"],
                    ),
                )
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "provider_governor_unavailable"
            ) from exc

    def record_429(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        retry_after_seconds: float | None = None,
        now: datetime | None = None,
    ) -> None:
        stamp = now or utc_now()
        now_s = _dt_to_db(stamp)
        delay = float(retry_after_seconds) if retry_after_seconds is not None else 30.0
        until = _dt_to_db(stamp + timedelta(seconds=max(1.0, delay)))
        try:
            with self._tx() as conn:
                row = self._ensure_state(conn, provider_id, model_id, now_s)
                failures = int(row["failure_count"] or 0) + 1
                breaker = row["breaker_state"]
                opened = row["opened_at"]
                if failures >= self.limits.failure_threshold:
                    breaker = STATE_OPEN
                    opened = now_s
                conn.execute(
                    """
                    UPDATE provider_governor_state SET
                        throttle_until = ?, failure_count = ?, breaker_state = ?,
                        opened_at = ?, updated_at = ?
                    WHERE state_key = ?
                    """,
                    (until, failures, breaker, opened, now_s, row["state_key"]),
                )
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "provider_governor_unavailable"
            ) from exc

    def record_success(self, provider_id: str, model_id: str = "") -> None:
        stamp = utc_now()
        now_s = _dt_to_db(stamp)
        try:
            with self._tx() as conn:
                row = self._ensure_state(conn, provider_id, model_id, now_s)
                conn.execute(
                    """
                    UPDATE provider_governor_state SET
                        breaker_state = 'CLOSED', failure_count = 0,
                        opened_at = NULL, half_open_probes = 0, updated_at = ?
                    WHERE state_key = ?
                    """,
                    (now_s, row["state_key"]),
                )
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "provider_governor_unavailable"
            ) from exc

    def record_failure(
        self, provider_id: str, model_id: str = "", *, error_code: str = ""
    ) -> None:
        if error_code in NON_QUALIFYING_ERRORS:
            return
        stamp = utc_now()
        now_s = _dt_to_db(stamp)
        try:
            with self._tx() as conn:
                row = self._ensure_state(conn, provider_id, model_id, now_s)
                failures = int(row["failure_count"] or 0) + 1
                breaker = row["breaker_state"]
                opened = row["opened_at"]
                probes = int(row["half_open_probes"] or 0)
                if breaker == STATE_HALF_OPEN:
                    breaker = STATE_OPEN
                    opened = now_s
                    probes = 0
                elif failures >= self.limits.failure_threshold:
                    breaker = STATE_OPEN
                    opened = now_s
                conn.execute(
                    """
                    UPDATE provider_governor_state SET
                        failure_count = ?, breaker_state = ?, opened_at = ?,
                        half_open_probes = ?, updated_at = ?
                    WHERE state_key = ?
                    """,
                    (failures, breaker, opened, probes, now_s, row["state_key"]),
                )
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "provider_governor_unavailable"
            ) from exc

    def breaker_state(self, provider_id: str, model_id: str = "") -> str:
        stamp = utc_now()
        now_s = _dt_to_db(stamp)
        try:
            with self._tx() as conn:
                row = self._ensure_state(conn, provider_id, model_id, now_s)
                row = self._maybe_transition_row(conn, row, stamp, now_s)
                return str(row["breaker_state"])
        except sqlite3.Error:
            return STATE_CLOSED

    def is_available(
        self, provider_id: str, model_id: str = "", *, lane: str = LANE_BACKGROUND
    ) -> bool:
        try:
            slot = self.acquire(
                provider_id=provider_id, model_id=model_id, lane=lane, worker_id="probe"
            )
            self.release(slot)
            return True
        except ProviderCapacityUnavailable:
            return False

    def close(self) -> None:
        with self._lock:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None


class ProviderGovernor:
    """Facade used by workers / router."""

    def __init__(
        self,
        store: ProviderGovernorStore | None = None,
        limits: GovernorLimits | None = None,
        *,
        observability=None,
    ):
        self.limits = limits or GovernorLimits()
        self.store = store or InMemoryProviderGovernorStore(self.limits)
        self.observability = observability

    @staticmethod
    def _pop_lineage(kwargs: dict) -> dict:
        """Strip request-local obs identity before store calls (never stored on self)."""
        lineage = {}
        for key in (
            "envelope",
            "parent_context",
            "workflow_id",
            "task_id",
            "tenant_id",
            "actor_ref",
            "correlation_id",
        ):
            if key in kwargs:
                lineage[key] = kwargs.pop(key)
        return lineage

    def _lineage_emit_kw(
        self,
        *,
        envelope=None,
        parent_context=None,
        task_id: str = "",
        tenant_id: str | None = None,
        workflow_id: str | None = None,
        actor_ref: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        if envelope is not None:
            return {
                "envelope": envelope,
                "parent_context": parent_context,
                "task_id": str(getattr(envelope, "task_id", None) or task_id or ""),
                "workflow_id": str(getattr(envelope, "workflow_id", "") or ""),
                "tenant_id": str(getattr(envelope, "tenant_id", "") or ""),
                "actor_ref": str(getattr(envelope, "actor_ref", "") or ""),
                "correlation_id": str(
                    getattr(envelope, "correlation_id", None) or correlation_id or ""
                )
                or None,
            }
        return {
            "envelope": None,
            "parent_context": parent_context,
            "task_id": str(task_id or ""),
            "workflow_id": str(workflow_id or ""),
            "tenant_id": str(tenant_id or ""),
            "actor_ref": str(actor_ref or ""),
            "correlation_id": correlation_id,
        }

    def _resolve_obs_context(
        self,
        *,
        parent_context=None,
        envelope=None,
        task_id: str = "",
        workflow_id: str = "",
        tenant_id: str = "",
        actor_ref: str = "",
        correlation_id: str | None = None,
    ):
        """Prefer parent/envelope lineage; never invent competing root when parent exists."""
        obs = self.observability
        if obs is None:
            return None

        if parent_context is not None:
            return obs.child_span(
                parent_context,
                workflow_id=workflow_id or None,
                task_id=task_id or None,
                tenant_id=tenant_id or None,
                actor_ref=actor_ref or None,
            )

        if envelope is not None:
            env_workflow = str(getattr(envelope, "workflow_id", "") or "")
            env_task = str(getattr(envelope, "task_id", "") or task_id or "")
            env_tenant = str(getattr(envelope, "tenant_id", "") or "")
            env_actor = str(getattr(envelope, "actor_ref", "") or "")
            existing = (
                obs.context_for_workflow(env_workflow) if env_workflow else None
            )
            if existing is not None:
                return existing.child(
                    task_id=env_task or existing.task_id,
                    actor_ref=env_actor or None,
                    tenant_id=env_tenant or None,
                )
            from observability.context import ObservabilityContext

            return ObservabilityContext(
                correlation_id=str(envelope.correlation_id),
                trace_id=str(envelope.trace_id),
                span_id=str(uuid.uuid4()),
                parent_span_id=None,
                workflow_id=env_workflow,
                task_id=env_task,
                actor_ref=env_actor,
                tenant_id=env_tenant,
            )

        resolved_workflow = str(workflow_id or "")
        if resolved_workflow:
            existing = obs.context_for_workflow(resolved_workflow)
            if existing is not None:
                return existing.child(
                    task_id=task_id or existing.task_id,
                    actor_ref=actor_ref or None,
                    tenant_id=tenant_id or None,
                )

        return obs.create_context(
            correlation_id=correlation_id,
            workflow_id=resolved_workflow,
            task_id=str(task_id or ""),
            actor_ref=str(actor_ref or ""),
            tenant_id=str(tenant_id or ""),
        )

    def _emit(self, event_type: str, **kwargs) -> None:
        obs = self.observability
        if obs is None:
            return
        from observability.helpers import safe_emit

        parent_context = kwargs.pop("parent_context", None)
        envelope = kwargs.pop("envelope", None)
        task_id = str(kwargs.pop("task_id", "") or "")
        workflow_id = str(kwargs.pop("workflow_id", "") or "")
        tenant_id = str(kwargs.pop("tenant_id", "") or "")
        actor_ref = str(kwargs.pop("actor_ref", "") or "")
        correlation_id = kwargs.pop("correlation_id", None)
        context = self._resolve_obs_context(
            parent_context=parent_context,
            envelope=envelope,
            task_id=task_id,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            actor_ref=actor_ref,
            correlation_id=correlation_id,
        )
        safe_emit(
            obs,
            event_type,
            context=context,
            component="provider_governor",
            provider=str(kwargs.pop("provider", "") or ""),
            model=str(kwargs.pop("model", "") or ""),
            status=str(kwargs.pop("status", "") or ""),
            error_code=kwargs.pop("error_code", None),
            metadata=kwargs.pop("metadata", None),
        )

    def acquire(self, **kwargs) -> str:
        lineage_raw = self._pop_lineage(kwargs)
        lineage = self._lineage_emit_kw(**lineage_raw)
        try:
            slot = self.store.acquire(**kwargs)
            self._emit(
                "provider.capacity_acquired",
                provider=kwargs.get("provider_id", ""),
                model=kwargs.get("model_id", ""),
                status="acquired",
                metadata={"lane": kwargs.get("lane"), "slot_id": slot},
                **lineage,
            )
            return slot
        except ProviderCapacityUnavailable as exc:
            self._emit(
                "provider.throttle",
                provider=kwargs.get("provider_id", ""),
                model=kwargs.get("model_id", ""),
                status="throttled",
                error_code=exc.reason,
                metadata={"lane": kwargs.get("lane")},
                **lineage,
            )
            raise

    def admit(
        self,
        *,
        provider_id: str,
        model_id: str = "",
        lane: str = LANE_INTERACTIVE,
        envelope=None,
        now: datetime | None = None,
        **kwargs,
    ) -> str:
        stamp = now or utc_now()
        lineage_raw = {"envelope": envelope} if envelope is not None else {}
        for key in (
            "parent_context",
            "workflow_id",
            "task_id",
            "tenant_id",
            "actor_ref",
            "correlation_id",
        ):
            if key in kwargs:
                lineage_raw[key] = kwargs[key]
        lineage = self._lineage_emit_kw(**lineage_raw)
        deadline = getattr(envelope, "deadline_at", None) if envelope is not None else None
        if deadline is not None:
            due = deadline
            if getattr(due, "tzinfo", None) is None:
                due = due.replace(tzinfo=timezone.utc)
            check = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)
            if check >= due:
                exc = ProviderCapacityUnavailable("provider_deadline_expired")
                self._emit(
                    "provider.backpressure",
                    provider=provider_id,
                    model=model_id,
                    status="rejected",
                    error_code=exc.reason,
                    metadata={"lane": lane},
                    **lineage,
                )
                raise exc
        try:
            slot = self.acquire(
                provider_id=provider_id,
                model_id=model_id,
                lane=lane,
                envelope=envelope,
                now=stamp,
                **kwargs,
            )
            self._emit(
                "provider.admission",
                provider=provider_id,
                model=model_id,
                status="admitted",
                metadata={"lane": lane, "slot_id": slot},
                **lineage,
            )
            return slot
        except ProviderCapacityUnavailable as exc:
            self._emit(
                "provider.backpressure",
                provider=provider_id,
                model=model_id,
                status="rejected",
                error_code=exc.reason,
                metadata={"lane": lane},
                **lineage,
            )
            raise

    def release(self, slot_id: str, **kwargs) -> None:
        lineage_raw = self._pop_lineage(kwargs)
        lineage = self._lineage_emit_kw(**lineage_raw)
        self.store.release(slot_id)
        self._emit(
            "provider.capacity_released",
            status="released",
            metadata={"slot_id": slot_id},
            **lineage,
        )

    def record_tokens(self, *args, **kwargs) -> None:
        lineage_raw = self._pop_lineage(kwargs)
        self._lineage_emit_kw(**lineage_raw)
        self.store.record_tokens(*args, **kwargs)

    def record_429(self, *args, **kwargs) -> None:
        lineage_raw = self._pop_lineage(kwargs)
        lineage = self._lineage_emit_kw(**lineage_raw)
        self.store.record_429(*args, **kwargs)
        self._emit(
            "provider.429",
            provider=args[0] if args else kwargs.get("provider_id", ""),
            model=args[1] if len(args) > 1 else kwargs.get("model_id", ""),
            status="throttled",
            metadata={"retry_after": kwargs.get("retry_after_seconds")},
            **lineage,
        )

    def record_success(self, *args, **kwargs) -> None:
        self._pop_lineage(kwargs)
        self.store.record_success(*args, **kwargs)

    def record_failure(self, *args, **kwargs) -> None:
        lineage_raw = self._pop_lineage(kwargs)
        lineage = self._lineage_emit_kw(**lineage_raw)
        self.store.record_failure(*args, **kwargs)
        self._emit(
            "provider.breaker",
            provider=args[0] if args else "",
            status=self.store.breaker_state(
                args[0] if args else "", args[1] if len(args) > 1 else ""
            ),
            error_code=kwargs.get("error_code"),
            **lineage,
        )

    def breaker_state(self, provider_id: str, model_id: str = "") -> str:
        return self.store.breaker_state(provider_id, model_id)

    def is_available(self, provider_id: str, model_id: str = "", *, lane: str = LANE_BACKGROUND) -> bool:
        return self.store.is_available(provider_id, model_id, lane=lane)


def parse_retry_after(value) -> float | None:
    if value is None:
        return None
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return None
