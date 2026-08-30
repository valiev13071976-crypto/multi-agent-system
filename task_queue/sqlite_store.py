"""SQLite-backed durable TaskQueue store with atomic claim."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.sqlite_store import SqliteConnection
from task_queue.lanes import (
    BACKGROUND_POOL_LANES,
    DEFAULT_LANE,
    LANE_INTERACTIVE,
    LaneCapacityConfig,
    effective_priority_rank,
    is_interactive_lane,
    normalize_lane,
)
from task_queue.models import (
    STATUS_DEAD_LETTERED,
    STATUS_LEASED,
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_RUNNING,
    QueueTask,
    utc_now,
)
from task_queue.store import TaskQueueStore


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


_PRIORITY_CASE = """
CASE priority
  WHEN 'critical' THEN 3
  WHEN 'high' THEN 2
  WHEN 'normal' THEN 1
  WHEN 'low' THEN 0
  ELSE 0
END
"""


class PersistentTaskQueueStore(TaskQueueStore):
    """Durable queue_tasks (schema v5+) with CAS claim / lease fencing."""

    def __init__(self, connection: SqliteConnection):
        self._connection = connection
        self.sqlite_busy_count = 0
        self.capacity_throttle_count = 0

    def diagnostics(self) -> dict:
        return {
            "sqlite_busy_count": int(self.sqlite_busy_count),
            "capacity_throttle_count": int(self.capacity_throttle_count),
        }

    def enqueue(self, task: QueueTask) -> None:
        self.save(task)

    def get(self, queue_task_id: str) -> QueueTask | None:
        conn = self._connection.connect()
        row = conn.execute(
            "SELECT * FROM queue_tasks WHERE queue_task_id = ?",
            (queue_task_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def get_for_tenant(self, queue_task_id: str, tenant_id: str) -> QueueTask | None:
        tid = str(tenant_id or "").strip()
        if not tid:
            return None
        task = self.get(queue_task_id)
        if task is None:
            return None
        if str(getattr(task, "tenant_id", "") or "").strip() != tid:
            return None
        return task

    def save(self, task: QueueTask) -> None:
        now = utc_now()
        updated = task.updated_at or now
        conn = self._connection.connect()
        try:
            existing = conn.execute(
                "SELECT queue_task_id FROM queue_tasks WHERE queue_task_id = ?",
                (task.queue_task_id,),
            ).fetchone()
            payload = (
                task.queue_task_id,
                task.workflow_id,
                task.task_id,
                task.execution_key,
                task.tenant_id or "",
                task.user_id or "",
                task.actor_ref or "",
                task.status,
                task.priority,
                normalize_lane(task.execution_lane),
                int(task.attempt),
                int(task.max_attempts),
                _dt_to_db(task.available_at),
                _dt_to_db(task.created_at),
                _dt_to_db(updated),
                _dt_to_db(task.started_at),
                _dt_to_db(task.completed_at),
                _dt_to_db(task.failed_at),
                task.timeout_seconds,
                task.error_code,
                json.dumps(dict(task.metadata or {}), separators=(",", ":"), default=str),
                task.worker_id,
                task.lease_id,
                _dt_to_db(task.leased_at),
                _dt_to_db(task.lease_expires_at),
            )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO queue_tasks (
                        queue_task_id, workflow_id, task_id, execution_key,
                        tenant_id, user_id, actor_ref, status, priority, execution_lane,
                        attempt, max_attempts, available_at, created_at, updated_at,
                        started_at, completed_at, failed_at, timeout_seconds,
                        error_code, metadata_json, worker_id, lease_id,
                        leased_at, lease_expires_at, row_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    payload,
                )
            else:
                conn.execute(
                    """
                    UPDATE queue_tasks SET
                        workflow_id=?, task_id=?, execution_key=?,
                        tenant_id=?, user_id=?, actor_ref=?, status=?, priority=?,
                        execution_lane=?,
                        attempt=?, max_attempts=?, available_at=?, created_at=?, updated_at=?,
                        started_at=?, completed_at=?, failed_at=?, timeout_seconds=?,
                        error_code=?, metadata_json=?, worker_id=?, lease_id=?,
                        leased_at=?, lease_expires_at=?, row_version=row_version+1
                    WHERE queue_task_id=?
                    """,
                    (
                        task.workflow_id,
                        task.task_id,
                        task.execution_key,
                        task.tenant_id or "",
                        task.user_id or "",
                        task.actor_ref or "",
                        task.status,
                        task.priority,
                        normalize_lane(task.execution_lane),
                        int(task.attempt),
                        int(task.max_attempts),
                        _dt_to_db(task.available_at),
                        _dt_to_db(task.created_at),
                        _dt_to_db(updated),
                        _dt_to_db(task.started_at),
                        _dt_to_db(task.completed_at),
                        _dt_to_db(task.failed_at),
                        task.timeout_seconds,
                        task.error_code,
                        json.dumps(
                            dict(task.metadata or {}), separators=(",", ":"), default=str
                        ),
                        task.worker_id,
                        task.lease_id,
                        _dt_to_db(task.leased_at),
                        _dt_to_db(task.lease_expires_at),
                        task.queue_task_id,
                    ),
                )
            self._connection.maybe_autocommit()
        except sqlite3.Error as exc:
            raise SideEffectPersistenceUnavailableError(
                "queue_persistence_unavailable"
            ) from exc

    def list_ready(self) -> tuple[QueueTask, ...]:
        # Filtering by available_at / lease expiry is done in TaskQueue.list_ready.
        conn = self._connection.connect()
        rows = conn.execute(
            f"""
            SELECT * FROM queue_tasks
            WHERE status IN ('{STATUS_QUEUED}', '{STATUS_RETRY_WAIT}', '{STATUS_LEASED}')
            ORDER BY {_PRIORITY_CASE} DESC, available_at ASC, created_at ASC, queue_task_id ASC
            """
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_all(self) -> tuple[QueueTask, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM queue_tasks ORDER BY created_at ASC, queue_task_id ASC"
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def get_dead_letters(self) -> tuple[QueueTask, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM queue_tasks WHERE status = ? ORDER BY failed_at ASC",
            (STATUS_DEAD_LETTERED,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_by_execution_key(self, execution_key: str) -> tuple[QueueTask, ...]:
        conn = self._connection.connect()
        rows = conn.execute(
            "SELECT * FROM queue_tasks WHERE execution_key = ? ORDER BY created_at ASC",
            (execution_key,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def count_by_status(
        self, *, tenant_id: str = "", now: datetime | None = None
    ) -> dict:
        stamp = now or utc_now()
        stamp_s = _dt_to_db(stamp)
        conn = self._connection.connect()
        pending_global = int(
            conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM queue_tasks
                WHERE status IN ('{STATUS_QUEUED}', '{STATUS_RETRY_WAIT}')
                """
            ).fetchone()["c"]
        )
        running_global = int(
            conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM queue_tasks
                WHERE status = '{STATUS_RUNNING}'
                   OR (
                        status = '{STATUS_LEASED}'
                        AND (lease_expires_at IS NULL OR lease_expires_at > ?)
                   )
                """,
                (stamp_s,),
            ).fetchone()["c"]
        )
        pending_tenant = running_tenant = 0
        if tenant_id:
            pending_tenant = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS c FROM queue_tasks
                    WHERE tenant_id = ?
                      AND status IN ('{STATUS_QUEUED}', '{STATUS_RETRY_WAIT}')
                    """,
                    (tenant_id,),
                ).fetchone()["c"]
            )
            running_tenant = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS c FROM queue_tasks
                    WHERE tenant_id = ?
                      AND (
                        status = '{STATUS_RUNNING}'
                        OR (
                            status = '{STATUS_LEASED}'
                            AND (lease_expires_at IS NULL OR lease_expires_at > ?)
                        )
                      )
                    """,
                    (tenant_id, stamp_s),
                ).fetchone()["c"]
            )
        return {
            "pending_global": pending_global,
            "pending_tenant": pending_tenant,
            "running_global": running_global,
            "running_tenant": running_tenant,
            "pending_by_lane": {
                str(r["execution_lane"] or "background"): int(r["c"])
                for r in conn.execute(
                    f"""
                    SELECT COALESCE(execution_lane, 'background') AS execution_lane,
                           COUNT(*) AS c
                    FROM queue_tasks
                    WHERE status IN ('{STATUS_QUEUED}', '{STATUS_RETRY_WAIT}')
                    GROUP BY COALESCE(execution_lane, 'background')
                    """
                ).fetchall()
            },
            "running_by_lane": {
                str(r["execution_lane"] or "background"): int(r["c"])
                for r in conn.execute(
                    f"""
                    SELECT COALESCE(execution_lane, 'background') AS execution_lane,
                           COUNT(*) AS c
                    FROM queue_tasks
                    WHERE status = '{STATUS_RUNNING}'
                       OR (
                            status = '{STATUS_LEASED}'
                            AND (lease_expires_at IS NULL OR lease_expires_at > ?)
                       )
                    GROUP BY COALESCE(execution_lane, 'background')
                    """,
                    (stamp_s,),
                ).fetchall()
            },
        }

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        now: datetime | None = None,
        max_running_global: int | None = None,
        max_running_per_tenant: int | None = None,
        allowed_lanes: frozenset[str] | None = None,
        lane_config: LaneCapacityConfig | None = None,
    ) -> QueueTask | None:
        """Atomically claim one eligible task (BEGIN IMMEDIATE + CAS UPDATE).

        Block 2: lane filter, interactive reservation, bounded aging, tenant fairness.
        """

        stamp = now or utc_now()
        stamp_s = _dt_to_db(stamp)
        lease_id = str(uuid.uuid4())
        lease_until = stamp + timedelta(seconds=float(lease_seconds))
        lease_until_s = _dt_to_db(lease_until)
        worker = str(worker_id or "").strip() or "worker"
        cfg = lane_config or LaneCapacityConfig()
        lanes = allowed_lanes  # None → all lanes

        conn = self._connection.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Active running (non-expired leases)
            running_rows = conn.execute(
                f"""
                SELECT queue_task_id, tenant_id, execution_lane, status, lease_expires_at
                FROM queue_tasks
                WHERE status = '{STATUS_RUNNING}'
                   OR (
                        status = '{STATUS_LEASED}'
                        AND (lease_expires_at IS NULL OR lease_expires_at > ?)
                   )
                """,
                (stamp_s,),
            ).fetchall()
            running_global = len(running_rows)
            if max_running_global is not None and running_global >= int(max_running_global):
                self.capacity_throttle_count += 1
                conn.commit()
                return None

            def _lane(row) -> str:
                try:
                    return normalize_lane(row["execution_lane"])
                except (KeyError, IndexError):
                    return DEFAULT_LANE

            interactive_running = sum(
                1 for r in running_rows if _lane(r) == LANE_INTERACTIVE
            )
            background_running = running_global - interactive_running
            reserved = int(cfg.interactive_reserved)
            max_bg = None
            if max_running_global is not None:
                max_bg = max(0, int(max_running_global) - reserved)

            # Pending interactive (for borrow decision)
            interactive_pending = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS c FROM queue_tasks
                    WHERE status IN ('{STATUS_QUEUED}', '{STATUS_RETRY_WAIT}')
                      AND available_at <= ?
                      AND COALESCE(execution_lane, 'background') = ?
                    """,
                    (stamp_s, LANE_INTERACTIVE),
                ).fetchone()["c"]
            )

            tenant_running: dict[str, int] = {}
            for r in running_rows:
                tid = str(r["tenant_id"] or "")
                if tid:
                    tenant_running[tid] = tenant_running.get(tid, 0) + 1

            candidates = conn.execute(
                f"""
                SELECT queue_task_id, status, tenant_id, priority, created_at,
                       available_at, COALESCE(execution_lane, 'background') AS execution_lane
                FROM (
                    SELECT queue_task_id, status, tenant_id, priority, created_at,
                           available_at, COALESCE(execution_lane, 'background') AS execution_lane,
                           ROW_NUMBER() OVER (
                             PARTITION BY tenant_id
                             ORDER BY {_PRIORITY_CASE} DESC, available_at ASC,
                                      created_at ASC, queue_task_id ASC
                           ) AS tenant_rn
                    FROM queue_tasks
                    WHERE (
                        status IN ('{STATUS_QUEUED}', '{STATUS_RETRY_WAIT}')
                        AND available_at <= ?
                    ) OR (
                        status = '{STATUS_LEASED}'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?
                    )
                )
                WHERE tenant_rn <= 16
                ORDER BY {_PRIORITY_CASE} DESC, available_at ASC, created_at ASC, queue_task_id ASC
                LIMIT 128
                """,
                (stamp_s, stamp_s),
            ).fetchall()

            scored = []
            for row in candidates:
                lane = normalize_lane(row["execution_lane"])
                if lanes is not None and lane not in lanes:
                    continue
                tenant = str(row["tenant_id"] or "")
                if max_running_per_tenant is not None and tenant:
                    if tenant_running.get(tenant, 0) >= int(max_running_per_tenant):
                        continue
                # Lane capacity: background cannot consume interactive reservation
                # unless controlled borrow when no interactive pending.
                if lane in BACKGROUND_POOL_LANES and max_bg is not None:
                    if background_running >= max_bg:
                        can_borrow = (
                            cfg.background_may_borrow
                            and interactive_pending == 0
                            and (
                                max_running_global is None
                                or running_global < int(max_running_global)
                            )
                        )
                        if not can_borrow:
                            continue
                created = _dt_from_db(row["created_at"]) or stamp
                eff = effective_priority_rank(
                    str(row["priority"] or "normal"),
                    created_at=created,
                    now=stamp,
                    aging_seconds_per_step=cfg.aging_seconds_per_step,
                    aging_max_boost=cfg.aging_max_boost,
                )
                fair = tenant_running.get(tenant, 0) if cfg.fairness_enabled else 0
                scored.append(
                    (
                        -eff,
                        fair,
                        row["available_at"] or "",
                        row["created_at"] or "",
                        row["queue_task_id"],
                        row,
                        lane,
                    )
                )
            scored.sort()
            claimed_id = None
            for item in scored:
                row = item[5]
                qid = row["queue_task_id"]
                status = row["status"]
                if status in {STATUS_QUEUED, STATUS_RETRY_WAIT}:
                    cur = conn.execute(
                        """
                        UPDATE queue_tasks SET
                            status = ?,
                            worker_id = ?,
                            lease_id = ?,
                            leased_at = ?,
                            lease_expires_at = ?,
                            updated_at = ?,
                            row_version = row_version + 1
                        WHERE queue_task_id = ?
                          AND status = ?
                          AND available_at <= ?
                        """,
                        (
                            STATUS_LEASED,
                            worker,
                            lease_id,
                            stamp_s,
                            lease_until_s,
                            stamp_s,
                            qid,
                            status,
                            stamp_s,
                        ),
                    )
                else:
                    cur = conn.execute(
                        """
                        UPDATE queue_tasks SET
                            status = ?,
                            worker_id = ?,
                            lease_id = ?,
                            leased_at = ?,
                            lease_expires_at = ?,
                            updated_at = ?,
                            row_version = row_version + 1
                        WHERE queue_task_id = ?
                          AND status = ?
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= ?
                        """,
                        (
                            STATUS_LEASED,
                            worker,
                            lease_id,
                            stamp_s,
                            lease_until_s,
                            stamp_s,
                            qid,
                            STATUS_LEASED,
                            stamp_s,
                        ),
                    )
                if cur.rowcount == 1:
                    claimed_id = qid
                    break
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                self.sqlite_busy_count += 1
            raise SideEffectPersistenceUnavailableError(
                "queue_persistence_unavailable"
            ) from exc

        if claimed_id is None:
            return None
        return self.get(claimed_id)

    def heartbeat(
        self,
        queue_task_id: str,
        *,
        worker_id: str,
        lease_id: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> QueueTask | None:
        stamp = now or utc_now()
        stamp_s = _dt_to_db(stamp)
        lease_until_s = _dt_to_db(stamp + timedelta(seconds=float(lease_seconds)))
        conn = self._connection.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"""
                UPDATE queue_tasks SET
                    lease_expires_at = ?,
                    updated_at = ?,
                    row_version = row_version + 1
                WHERE queue_task_id = ?
                  AND worker_id = ?
                  AND lease_id = ?
                  AND status IN ('{STATUS_LEASED}', '{STATUS_RUNNING}')
                  AND (lease_expires_at IS NULL OR lease_expires_at > ?)
                """,
                (
                    lease_until_s,
                    stamp_s,
                    queue_task_id,
                    str(worker_id),
                    str(lease_id),
                    stamp_s,
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
                "queue_persistence_unavailable"
            ) from exc
        if not ok:
            return None
        return self.get(queue_task_id)

    def reclaim_expired_running(
        self,
        *,
        now: datetime | None = None,
        error_code: str = "worker_interrupted",
    ) -> tuple[str, ...]:
        """CAS demote RUNNING with expired/missing lease → RETRY_WAIT. Never live leases."""

        stamp = now or utc_now()
        stamp_s = _dt_to_db(stamp)
        conn = self._connection.connect()
        recovered: list[str] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT * FROM queue_tasks
                WHERE status = '{STATUS_RUNNING}'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (stamp_s,),
            ).fetchall()
            for row in rows:
                task = self._from_row(row)
                meta = dict(task.metadata or {})
                meta["recovered_from_running"] = True
                meta["recovery_reason"] = "lease_expired"
                cur = conn.execute(
                    f"""
                    UPDATE queue_tasks SET
                        status = ?,
                        error_code = COALESCE(error_code, ?),
                        failed_at = ?,
                        available_at = ?,
                        worker_id = NULL,
                        lease_id = NULL,
                        leased_at = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?,
                        metadata_json = ?,
                        row_version = row_version + 1
                    WHERE queue_task_id = ?
                      AND status = '{STATUS_RUNNING}'
                      AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    """,
                    (
                        STATUS_RETRY_WAIT,
                        error_code,
                        stamp_s,
                        stamp_s,
                        stamp_s,
                        json.dumps(meta, separators=(",", ":"), default=str),
                        task.queue_task_id,
                        stamp_s,
                    ),
                )
                if cur.rowcount == 1:
                    recovered.append(task.queue_task_id)
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise SideEffectPersistenceUnavailableError(
                "queue_persistence_unavailable"
            ) from exc
        return tuple(recovered)

    def mutate_if_lease(
        self,
        queue_task_id: str,
        *,
        lease_id: str,
        worker_id: str | None,
        now: datetime | None = None,
    ) -> QueueTask | None:
        """Load task only if lease ownership is still valid (fencing read)."""

        stamp = now or utc_now()
        task = self.get(queue_task_id)
        if task is None:
            return None
        if task.lease_id is None or task.lease_id != lease_id:
            return None
        if worker_id is not None and str(task.worker_id or "") != str(worker_id):
            return None
        if task.lease_expires_at is not None and task.lease_expires_at <= stamp:
            return None
        return task

    def _from_row(self, row: sqlite3.Row) -> QueueTask:
        meta: dict = {}
        raw = row["metadata_json"]
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    meta = loaded
            except json.JSONDecodeError:
                meta = {}
        return QueueTask(
            queue_task_id=row["queue_task_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            execution_key=row["execution_key"],
            status=row["status"],
            priority=row["priority"],
            attempt=int(row["attempt"] or 0),
            max_attempts=int(row["max_attempts"] or 1),
            created_at=_dt_from_db(row["created_at"]) or utc_now(),
            available_at=_dt_from_db(row["available_at"]) or utc_now(),
            started_at=_dt_from_db(row["started_at"]),
            completed_at=_dt_from_db(row["completed_at"]),
            failed_at=_dt_from_db(row["failed_at"]),
            timeout_seconds=(
                float(row["timeout_seconds"])
                if row["timeout_seconds"] is not None
                else None
            ),
            error_code=row["error_code"],
            metadata=meta,
            lease_id=row["lease_id"],
            leased_at=_dt_from_db(row["leased_at"]),
            lease_expires_at=_dt_from_db(row["lease_expires_at"]),
            worker_id=row["worker_id"],
            updated_at=_dt_from_db(row["updated_at"]),
            tenant_id=str(row["tenant_id"] or ""),
            user_id=str(row["user_id"] or ""),
            actor_ref=str(row["actor_ref"] or ""),
            execution_lane=normalize_lane(
                row["execution_lane"] if "execution_lane" in row.keys() else DEFAULT_LANE
            ),
        )
