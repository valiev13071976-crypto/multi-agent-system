"""Durable workflow schedule store — SQLite backend for WorkflowScheduler."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from security.tenant import require_tenant_id
from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.sqlite_store import SqliteConnection
from workflow.models import utc_now
from workflow.schedule import ScheduleState, ScheduleStore


def _dt_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _dt_from_db(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class ScheduleWindowClaim:
    state: ScheduleState
    claim_token: str
    claimed_window_at: datetime


class PersistentScheduleStore(ScheduleStore):
    """SQLite-backed schedule definitions with multi-process window claim."""

    def __init__(self, connection: SqliteConnection):
        self._connection = connection

    def get(self, schedule_id: str) -> ScheduleState | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM workflow_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def save(self, state: ScheduleState) -> None:
        payload = dict(state.payload or {})
        tenant = require_tenant_id(payload.get("tenant_id"))
        payload["tenant_id"] = tenant
        now = utc_now()
        conn = self._connection.connect()
        try:
            existing = conn.execute(
                "SELECT schedule_id FROM workflow_schedules WHERE schedule_id = ?",
                (state.schedule_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO workflow_schedules (
                        schedule_id, tenant_id, workflow_type, version, payload_json,
                        next_run_at, interval_seconds, enabled, last_enqueued_at,
                        last_execution_key, run_count, created_at, updated_at, row_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        state.schedule_id,
                        tenant,
                        state.workflow_type,
                        state.version,
                        json.dumps(payload, separators=(",", ":"), default=str),
                        _dt_to_db(state.next_run_at),
                        state.interval_seconds,
                        1 if state.enabled else 0,
                        _dt_to_db(state.last_enqueued_at),
                        state.last_execution_key,
                        int(state.run_count),
                        _dt_to_db(now),
                        _dt_to_db(now),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE workflow_schedules SET
                        tenant_id=?, workflow_type=?, version=?, payload_json=?,
                        next_run_at=?, interval_seconds=?, enabled=?,
                        last_enqueued_at=?, last_execution_key=?, run_count=?,
                        updated_at=?, row_version=row_version+1
                    WHERE schedule_id=?
                    """,
                    (
                        tenant,
                        state.workflow_type,
                        state.version,
                        json.dumps(payload, separators=(",", ":"), default=str),
                        _dt_to_db(state.next_run_at),
                        state.interval_seconds,
                        1 if state.enabled else 0,
                        _dt_to_db(state.last_enqueued_at),
                        state.last_execution_key,
                        int(state.run_count),
                        _dt_to_db(now),
                        state.schedule_id,
                    ),
                )
            self._connection.maybe_autocommit()
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "schedule_persistence_unavailable"
            ) from exc

    def list_due(self, now: datetime) -> tuple[ScheduleState, ...]:
        """Due schedules without an active (unexpired) claim."""
        conn = self._connection.connect()
        stamp = _dt_to_db(now)
        rows = conn.execute(
            """
            SELECT * FROM workflow_schedules
            WHERE enabled = 1 AND next_run_at <= ?
              AND (claim_until IS NULL OR claim_until <= ?)
            ORDER BY next_run_at ASC, schedule_id ASC
            """,
            (stamp, stamp),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_all(self) -> tuple[ScheduleState, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM workflow_schedules ORDER BY schedule_id ASC"
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_stale_claims(self, now: datetime) -> tuple[ScheduleWindowClaim, ...]:
        conn = self._connection.connect()
        stamp = _dt_to_db(now)
        rows = conn.execute(
            """
            SELECT * FROM workflow_schedules
            WHERE claim_token IS NOT NULL
              AND claim_until IS NOT NULL
              AND claim_until <= ?
              AND claimed_window_at IS NOT NULL
            ORDER BY schedule_id ASC
            """,
            (stamp,),
        ).fetchall()
        out = []
        for row in rows:
            window = _dt_from_db(row["claimed_window_at"])
            token = row["claim_token"]
            if window is None or not token:
                continue
            out.append(
                ScheduleWindowClaim(
                    state=self._from_row(row),
                    claim_token=str(token),
                    claimed_window_at=window,
                )
            )
        return tuple(out)

    def claim_due_window(
        self,
        schedule_id: str,
        *,
        expected_next_run_at: datetime,
        now: datetime | None = None,
        lease_seconds: float = 120.0,
        claim_token: str | None = None,
    ) -> ScheduleWindowClaim | None:
        """Atomic schedule-window claim. Only one worker wins a given next_run_at."""

        stamp = now or utc_now()
        token = claim_token or str(uuid.uuid4())
        claim_until = stamp + timedelta(seconds=float(lease_seconds))
        conn = self._connection.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE workflow_schedules SET
                    claim_token = ?,
                    claim_until = ?,
                    claimed_window_at = next_run_at,
                    updated_at = ?,
                    row_version = row_version + 1
                WHERE schedule_id = ?
                  AND enabled = 1
                  AND next_run_at = ?
                  AND (claim_until IS NULL OR claim_until <= ?)
                """,
                (
                    token,
                    _dt_to_db(claim_until),
                    _dt_to_db(stamp),
                    schedule_id,
                    _dt_to_db(expected_next_run_at),
                    _dt_to_db(stamp),
                ),
            )
            ok = cur.rowcount == 1
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise SideEffectPersistenceUnavailableError(
                "schedule_persistence_unavailable"
            ) from exc
        if not ok:
            return None
        state = self.get(schedule_id)
        if state is None:
            return None
        return ScheduleWindowClaim(
            state=state,
            claim_token=token,
            claimed_window_at=expected_next_run_at,
        )

    def complete_claimed_window(
        self,
        schedule_id: str,
        *,
        claim_token: str,
        claimed_window_at: datetime,
        execution_key: str,
        next_run_at: datetime,
        enabled: bool,
        now: datetime | None = None,
    ) -> ScheduleState | None:
        """Advance schedule after successful enqueue; clears claim fencing."""

        stamp = now or utc_now()
        conn = self._connection.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE workflow_schedules SET
                    next_run_at = ?,
                    enabled = ?,
                    last_enqueued_at = ?,
                    last_execution_key = ?,
                    run_count = run_count + 1,
                    claim_token = NULL,
                    claim_until = NULL,
                    claimed_window_at = NULL,
                    updated_at = ?,
                    row_version = row_version + 1
                WHERE schedule_id = ?
                  AND claim_token = ?
                  AND claimed_window_at = ?
                """,
                (
                    _dt_to_db(next_run_at),
                    1 if enabled else 0,
                    _dt_to_db(stamp),
                    execution_key,
                    _dt_to_db(stamp),
                    schedule_id,
                    claim_token,
                    _dt_to_db(claimed_window_at),
                ),
            )
            ok = cur.rowcount == 1
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise SideEffectPersistenceUnavailableError(
                "schedule_persistence_unavailable"
            ) from exc
        if not ok:
            return None
        return self.get(schedule_id)

    def delete(self, schedule_id: str) -> None:
        conn = self._connection.connect()
        try:
            conn.execute(
                "DELETE FROM workflow_schedules WHERE schedule_id = ?",
                (schedule_id,),
            )
            self._connection.maybe_autocommit()
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "schedule_persistence_unavailable"
            ) from exc

    def _from_row(self, row: sqlite3.Row) -> ScheduleState:
        payload = {}
        raw = row["payload_json"]
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    payload = loaded
            except json.JSONDecodeError:
                payload = {}
        tenant = str(row["tenant_id"] or "").strip()
        if tenant and not payload.get("tenant_id"):
            payload["tenant_id"] = tenant
        return ScheduleState(
            schedule_id=row["schedule_id"],
            workflow_type=row["workflow_type"],
            version=row["version"],
            payload=payload,
            next_run_at=_dt_from_db(row["next_run_at"]) or utc_now(),
            interval_seconds=(
                float(row["interval_seconds"])
                if row["interval_seconds"] is not None
                else None
            ),
            enabled=bool(int(row["enabled"] or 0)),
            last_enqueued_at=_dt_from_db(row["last_enqueued_at"]),
            last_execution_key=row["last_execution_key"],
            run_count=int(row["run_count"] or 0),
        )
