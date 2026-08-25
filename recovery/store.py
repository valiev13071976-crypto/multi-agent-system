"""In-memory + SQLite persistence for recovery cases/decisions."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from autonomy.models import sanitize_metadata
from recovery.models import (
    ACTIVE_CASE_STATUSES,
    TERMINAL_CASE_STATUSES,
    RecoveryCase,
    RecoveryDecision,
    RecoveryQueueJob,
    utc_now,
)


SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS recovery_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_cases (
    recovery_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    case_type TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_check_at TEXT,
    attempt INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    operator_decision TEXT,
    reconciliation_id TEXT,
    parent_recovery_id TEXT,
    tool_trust_level TEXT NOT NULL DEFAULT '',
    reversible INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_active_dedup
ON recovery_cases(execution_id, case_type)
WHERE status IN ('open','queued','checking','waiting_operator','waiting_approval');
CREATE TABLE IF NOT EXISTS recovery_decisions (
    decision_id TEXT PRIMARY KEY,
    recovery_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note_safe TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS recovery_queue (
    job_id TEXT PRIMARY KEY,
    recovery_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    priority TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    leased_at TEXT,
    completed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_queue_due
ON recovery_queue(status, scheduled_at);
"""


class RecoveryPersistenceUnavailableError(RuntimeError):
    def __init__(self, reason: str = "recovery_persistence_unavailable"):
        self.reason = reason
        super().__init__(reason)


class RecoveryConflictError(RuntimeError):
    def __init__(self, reason: str = "recovery_conflict"):
        self.reason = reason
        super().__init__(reason)


def _dt_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_db(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _json_dumps(value) -> str:
    return json.dumps(sanitize_metadata(value or {}), separators=(",", ":"), sort_keys=True)


class RecoveryCaseStore:
    def create(self, case: RecoveryCase) -> RecoveryCase:
        raise NotImplementedError

    def get(self, recovery_id: str) -> RecoveryCase | None:
        raise NotImplementedError

    def update(self, case: RecoveryCase, *, expected_version: int) -> RecoveryCase:
        raise NotImplementedError

    def find_active(self, execution_id: str, case_type: str) -> RecoveryCase | None:
        raise NotImplementedError

    def list_open(self) -> tuple[RecoveryCase, ...]:
        raise NotImplementedError

    def list_all(self) -> tuple[RecoveryCase, ...]:
        raise NotImplementedError

    def add_decision(self, decision: RecoveryDecision) -> RecoveryDecision:
        raise NotImplementedError

    def list_decisions(self, recovery_id: str) -> tuple[RecoveryDecision, ...]:
        raise NotImplementedError

    def save_queue_job(self, job: RecoveryQueueJob) -> RecoveryQueueJob:
        raise NotImplementedError

    def list_queue_jobs(self) -> tuple[RecoveryQueueJob, ...]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class InMemoryRecoveryCaseStore(RecoveryCaseStore):
    def __init__(self):
        self._lock = threading.RLock()
        self._cases: dict[str, RecoveryCase] = {}
        self._decisions: list[RecoveryDecision] = []
        self._queue: dict[str, RecoveryQueueJob] = {}
        self.available = True

    def create(self, case: RecoveryCase) -> RecoveryCase:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        with self._lock:
            existing = self.find_active(case.execution_id, case.case_type)
            if existing is not None:
                raise RecoveryConflictError("duplicate_active_recovery_case")
            if case.recovery_id in self._cases:
                raise RecoveryConflictError("recovery_exists")
            self._cases[case.recovery_id] = case
            return case

    def get(self, recovery_id: str) -> RecoveryCase | None:
        with self._lock:
            return self._cases.get(recovery_id)

    def update(self, case: RecoveryCase, *, expected_version: int) -> RecoveryCase:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        with self._lock:
            current = self._cases.get(case.recovery_id)
            if current is None:
                raise RecoveryConflictError("recovery_not_found")
            if current.version != expected_version:
                raise RecoveryConflictError("recovery_version_conflict")
            if current.status in TERMINAL_CASE_STATUSES and case.status not in TERMINAL_CASE_STATUSES:
                raise RecoveryConflictError("terminal_case_reopen_denied")
            updated = RecoveryCase(
                recovery_id=case.recovery_id,
                execution_id=case.execution_id,
                workflow_id=case.workflow_id,
                task_id=case.task_id,
                action_id=case.action_id,
                tool_id=case.tool_id,
                operation=case.operation,
                case_type=case.case_type,
                status=case.status,
                severity=case.severity,
                reason_code=case.reason_code,
                created_at=case.created_at,
                updated_at=case.updated_at,
                next_check_at=case.next_check_at,
                attempt=case.attempt,
                max_attempts=case.max_attempts,
                operator_decision=case.operator_decision,
                reconciliation_id=case.reconciliation_id,
                parent_recovery_id=case.parent_recovery_id,
                tool_trust_level=case.tool_trust_level,
                reversible=case.reversible,
                metadata_safe=dict(case.metadata_safe),
                version=current.version + 1,
            )
            self._cases[updated.recovery_id] = updated
            return updated

    def find_active(self, execution_id: str, case_type: str) -> RecoveryCase | None:
        with self._lock:
            for row in self._cases.values():
                if (
                    row.execution_id == execution_id
                    and row.case_type == case_type
                    and row.status in ACTIVE_CASE_STATUSES
                ):
                    return row
        return None

    def list_open(self) -> tuple[RecoveryCase, ...]:
        with self._lock:
            rows = [c for c in self._cases.values() if c.status in ACTIVE_CASE_STATUSES]
        return tuple(sorted(rows, key=lambda c: c.recovery_id))

    def list_all(self) -> tuple[RecoveryCase, ...]:
        with self._lock:
            return tuple(sorted(self._cases.values(), key=lambda c: c.recovery_id))

    def add_decision(self, decision: RecoveryDecision) -> RecoveryDecision:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        with self._lock:
            self._decisions.append(decision)
            return decision

    def list_decisions(self, recovery_id: str) -> tuple[RecoveryDecision, ...]:
        with self._lock:
            rows = [d for d in self._decisions if d.recovery_id == recovery_id]
        return tuple(rows)

    def save_queue_job(self, job: RecoveryQueueJob) -> RecoveryQueueJob:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        with self._lock:
            self._queue[job.job_id] = job
            return job

    def list_queue_jobs(self) -> tuple[RecoveryQueueJob, ...]:
        with self._lock:
            return tuple(sorted(self._queue.values(), key=lambda j: j.job_id))


class SqliteRecoveryCaseStore(RecoveryCaseStore):
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self.available = True
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(DDL)
                conn.execute(
                    "INSERT OR REPLACE INTO recovery_schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                conn.commit()
            except Exception as exc:
                raise RecoveryPersistenceUnavailableError() from exc

    def close(self) -> None:
        with self._lock:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None

    def _row_to_case(self, row: sqlite3.Row) -> RecoveryCase:
        return RecoveryCase(
            recovery_id=row["recovery_id"],
            execution_id=row["execution_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            action_id=row["action_id"],
            tool_id=row["tool_id"],
            operation=row["operation"],
            case_type=row["case_type"],
            status=row["status"],
            severity=row["severity"],
            reason_code=row["reason_code"],
            created_at=_dt_from_db(row["created_at"]),
            updated_at=_dt_from_db(row["updated_at"]),
            next_check_at=_dt_from_db(row["next_check_at"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            operator_decision=row["operator_decision"],
            reconciliation_id=row["reconciliation_id"],
            parent_recovery_id=row["parent_recovery_id"],
            tool_trust_level=row["tool_trust_level"] or "",
            reversible=bool(row["reversible"]),
            metadata_safe=json.loads(row["metadata_json"] or "{}"),
            version=int(row["version"]),
        )

    def create(self, case: RecoveryCase) -> RecoveryCase:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        with self._lock:
            if self.find_active(case.execution_id, case.case_type) is not None:
                raise RecoveryConflictError("duplicate_active_recovery_case")
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO recovery_cases(
                        recovery_id, execution_id, workflow_id, task_id, action_id,
                        tool_id, operation, case_type, status, severity, reason_code,
                        created_at, updated_at, next_check_at, attempt, max_attempts,
                        operator_decision, reconciliation_id, parent_recovery_id,
                        tool_trust_level, reversible, metadata_json, version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        case.recovery_id,
                        case.execution_id,
                        case.workflow_id,
                        case.task_id,
                        case.action_id,
                        case.tool_id,
                        case.operation,
                        case.case_type,
                        case.status,
                        case.severity,
                        case.reason_code,
                        _dt_to_db(case.created_at),
                        _dt_to_db(case.updated_at),
                        _dt_to_db(case.next_check_at),
                        case.attempt,
                        case.max_attempts,
                        case.operator_decision,
                        case.reconciliation_id,
                        case.parent_recovery_id,
                        case.tool_trust_level,
                        1 if case.reversible else 0,
                        _json_dumps(dict(case.metadata_safe)),
                        case.version,
                    ),
                )
                conn.commit()
                return case
            except sqlite3.IntegrityError as exc:
                raise RecoveryConflictError("duplicate_active_recovery_case") from exc
            except sqlite3.Error as exc:
                raise RecoveryPersistenceUnavailableError() from exc

    def get(self, recovery_id: str) -> RecoveryCase | None:
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM recovery_cases WHERE recovery_id=?",
                (recovery_id,),
            ).fetchone()
            return self._row_to_case(row) if row else None

    def update(self, case: RecoveryCase, *, expected_version: int) -> RecoveryCase:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        with self._lock:
            current = self.get(case.recovery_id)
            if current is None:
                raise RecoveryConflictError("recovery_not_found")
            if current.version != expected_version:
                raise RecoveryConflictError("recovery_version_conflict")
            if current.status in TERMINAL_CASE_STATUSES and case.status not in TERMINAL_CASE_STATUSES:
                raise RecoveryConflictError("terminal_case_reopen_denied")
            new_version = current.version + 1
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE recovery_cases SET
                        status=?, severity=?, reason_code=?, updated_at=?, next_check_at=?,
                        attempt=?, operator_decision=?, reconciliation_id=?, metadata_json=?,
                        version=?
                    WHERE recovery_id=? AND version=?
                    """,
                    (
                        case.status,
                        case.severity,
                        case.reason_code,
                        _dt_to_db(case.updated_at),
                        _dt_to_db(case.next_check_at),
                        case.attempt,
                        case.operator_decision,
                        case.reconciliation_id,
                        _json_dumps(dict(case.metadata_safe)),
                        new_version,
                        case.recovery_id,
                        expected_version,
                    ),
                )
                if cur.rowcount != 1:
                    raise RecoveryConflictError("recovery_version_conflict")
                conn.commit()
            except RecoveryConflictError:
                raise
            except sqlite3.Error as exc:
                raise RecoveryPersistenceUnavailableError() from exc
            updated = self.get(case.recovery_id)
            assert updated is not None
            return updated

    def find_active(self, execution_id: str, case_type: str) -> RecoveryCase | None:
        with self._lock:
            rows = self._connect().execute(
                """
                SELECT * FROM recovery_cases
                WHERE execution_id=? AND case_type=?
                  AND status IN ('open','queued','checking','waiting_operator','waiting_approval')
                LIMIT 1
                """,
                (execution_id, case_type),
            ).fetchall()
            return self._row_to_case(rows[0]) if rows else None

    def list_open(self) -> tuple[RecoveryCase, ...]:
        with self._lock:
            rows = self._connect().execute(
                """
                SELECT * FROM recovery_cases
                WHERE status IN ('open','queued','checking','waiting_operator','waiting_approval')
                ORDER BY recovery_id
                """
            ).fetchall()
            return tuple(self._row_to_case(r) for r in rows)

    def list_all(self) -> tuple[RecoveryCase, ...]:
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM recovery_cases ORDER BY recovery_id"
            ).fetchall()
            return tuple(self._row_to_case(r) for r in rows)

    def add_decision(self, decision: RecoveryDecision) -> RecoveryDecision:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO recovery_decisions(
                        decision_id, recovery_id, decision, actor_id, reason_code,
                        created_at, note_safe, metadata_json
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        decision.decision_id,
                        decision.recovery_id,
                        decision.decision,
                        decision.actor_id,
                        decision.reason_code,
                        _dt_to_db(decision.created_at),
                        decision.note_safe,
                        _json_dumps(dict(decision.metadata_safe)),
                    ),
                )
                conn.commit()
                return decision
            except sqlite3.Error as exc:
                raise RecoveryPersistenceUnavailableError() from exc

    def list_decisions(self, recovery_id: str) -> tuple[RecoveryDecision, ...]:
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM recovery_decisions WHERE recovery_id=? ORDER BY created_at",
                (recovery_id,),
            ).fetchall()
            out = []
            for row in rows:
                out.append(
                    RecoveryDecision(
                        decision_id=row["decision_id"],
                        recovery_id=row["recovery_id"],
                        decision=row["decision"],
                        actor_id=row["actor_id"],
                        reason_code=row["reason_code"],
                        created_at=_dt_from_db(row["created_at"]),
                        note_safe=row["note_safe"] or "",
                        metadata_safe=json.loads(row["metadata_json"] or "{}"),
                    )
                )
            return tuple(out)

    def _row_to_job(self, row: sqlite3.Row) -> RecoveryQueueJob:
        return RecoveryQueueJob(
            job_id=row["job_id"],
            recovery_id=row["recovery_id"],
            action_type=row["action_type"],
            scheduled_at=_dt_from_db(row["scheduled_at"]),
            priority=row["priority"],
            attempt=int(row["attempt"]),
            status=row["status"],
            leased_at=_dt_from_db(row["leased_at"]),
            completed_at=_dt_from_db(row["completed_at"]),
            metadata_safe=json.loads(row["metadata_json"] or "{}"),
            version=int(row["version"]),
        )

    def save_queue_job(self, job: RecoveryQueueJob) -> RecoveryQueueJob:
        if not self.available:
            raise RecoveryPersistenceUnavailableError()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO recovery_queue(
                        job_id, recovery_id, action_type, scheduled_at, priority,
                        attempt, status, leased_at, completed_at, metadata_json, version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        recovery_id=excluded.recovery_id,
                        action_type=excluded.action_type,
                        scheduled_at=excluded.scheduled_at,
                        priority=excluded.priority,
                        attempt=excluded.attempt,
                        status=excluded.status,
                        leased_at=excluded.leased_at,
                        completed_at=excluded.completed_at,
                        metadata_json=excluded.metadata_json,
                        version=excluded.version
                    """,
                    (
                        job.job_id,
                        job.recovery_id,
                        job.action_type,
                        _dt_to_db(job.scheduled_at),
                        job.priority,
                        job.attempt,
                        job.status,
                        _dt_to_db(job.leased_at),
                        _dt_to_db(job.completed_at),
                        _json_dumps(dict(job.metadata_safe)),
                        job.version,
                    ),
                )
                conn.commit()
                return job
            except sqlite3.Error as exc:
                raise RecoveryPersistenceUnavailableError() from exc

    def list_queue_jobs(self) -> tuple[RecoveryQueueJob, ...]:
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM recovery_queue ORDER BY job_id"
            ).fetchall()
            return tuple(self._row_to_job(r) for r in rows)
