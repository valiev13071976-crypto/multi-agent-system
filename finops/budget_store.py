"""SQLite + in-memory persistence for budget reservations / ledger aggregates."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from autonomy.models import sanitize_metadata
from finops.budget_models import (
    ACTIVE_RESERVATION_STATUSES,
    RES_COMMITTED,
    RES_EXPIRED,
    RES_RECONCILED,
    RES_RELEASED,
    RES_RESERVED,
    RES_UNCERTAIN,
    BudgetReservation,
    utc_now,
)


SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS finops_budget_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finops_budget_policies (
    policy_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT '',
    hard_limit TEXT,
    soft_limit TEXT,
    currency TEXT NOT NULL,
    window TEXT NOT NULL,
    degrade_threshold TEXT,
    enabled INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    version INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS finops_budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    scope_refs_json TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    estimated_cost TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    committed_at TEXT,
    released_at TEXT,
    actual_cost TEXT,
    usage_record_key TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS finops_budget_ledger (
    scope_ref TEXT PRIMARY KEY,
    reserved TEXT NOT NULL DEFAULT '0',
    committed TEXT NOT NULL DEFAULT '0',
    spent TEXT NOT NULL DEFAULT '0',
    version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_finops_budget_res_status
ON finops_budget_reservations(status);
CREATE INDEX IF NOT EXISTS idx_finops_budget_res_task
ON finops_budget_reservations(task_id);
"""


class BudgetPersistenceUnavailableError(RuntimeError):
    def __init__(self, reason: str = "budget_persistence_unavailable"):
        self.reason = reason
        super().__init__(reason)


class BudgetReservationConflictError(RuntimeError):
    def __init__(self, reason: str = "budget_reservation_conflict"):
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


def _dec(value) -> Decimal:
    return Decimal(str(value or "0"))


def _json_dumps(value) -> str:
    return json.dumps(sanitize_metadata(value or {}), separators=(",", ":"), sort_keys=True)


class BudgetStore:
    def insert_reservation(self, reservation: BudgetReservation) -> BudgetReservation:
        raise NotImplementedError

    def get_reservation(self, reservation_id: str) -> BudgetReservation | None:
        raise NotImplementedError

    def update_reservation(
        self, reservation: BudgetReservation, *, expected_version: int | None = None
    ) -> BudgetReservation:
        raise NotImplementedError

    def list_reservations(
        self, *, status: str | None = None, task_id: str | None = None
    ) -> tuple[BudgetReservation, ...]:
        raise NotImplementedError

    def list_active(self, *, now: datetime | None = None) -> tuple[BudgetReservation, ...]:
        raise NotImplementedError

    def add_reserved(self, scope_ref: str, amount: Decimal) -> None:
        raise NotImplementedError

    def release_reserved(self, scope_ref: str, amount: Decimal) -> None:
        raise NotImplementedError

    def add_spent(self, scope_ref: str, amount: Decimal) -> None:
        raise NotImplementedError

    def get_totals(self, scope_ref: str) -> tuple[Decimal, Decimal, Decimal]:
        raise NotImplementedError

    def begin_reserve_transaction(self):
        return nullcontext()

    def close(self) -> None:
        return None


class nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class InMemoryBudgetStore(BudgetStore):
    def __init__(self):
        self._lock = threading.RLock()
        self._reservations: dict[str, BudgetReservation] = {}
        self._ledger: dict[str, dict[str, Decimal]] = {}

    def begin_reserve_transaction(self):
        return self._lock

    def insert_reservation(self, reservation: BudgetReservation) -> BudgetReservation:
        with self._lock:
            if reservation.reservation_id in self._reservations:
                raise BudgetReservationConflictError("reservation_exists")
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def get_reservation(self, reservation_id: str) -> BudgetReservation | None:
        with self._lock:
            return self._reservations.get(reservation_id)

    def update_reservation(
        self, reservation: BudgetReservation, *, expected_version: int | None = None
    ) -> BudgetReservation:
        with self._lock:
            current = self._reservations.get(reservation.reservation_id)
            if current is None:
                raise BudgetReservationConflictError("reservation_not_found")
            if expected_version is not None and current.version != expected_version:
                raise BudgetReservationConflictError("reservation_version_conflict")
            updated = BudgetReservation(
                reservation_id=reservation.reservation_id,
                scope_refs=reservation.scope_refs,
                task_id=reservation.task_id,
                provider=reservation.provider,
                model=reservation.model,
                estimated_cost=reservation.estimated_cost,
                currency=reservation.currency,
                status=reservation.status,
                created_at=reservation.created_at,
                expires_at=reservation.expires_at,
                agent_id=reservation.agent_id,
                committed_at=reservation.committed_at,
                released_at=reservation.released_at,
                actual_cost=reservation.actual_cost,
                usage_record_key=reservation.usage_record_key,
                metadata_safe=dict(reservation.metadata_safe),
                version=current.version + 1,
            )
            self._reservations[updated.reservation_id] = updated
            return updated

    def list_reservations(
        self, *, status: str | None = None, task_id: str | None = None
    ) -> tuple[BudgetReservation, ...]:
        with self._lock:
            rows = list(self._reservations.values())
        if status is not None:
            rows = [r for r in rows if r.status == status]
        if task_id is not None:
            rows = [r for r in rows if r.task_id == task_id]
        return tuple(sorted(rows, key=lambda r: r.reservation_id))

    def list_active(self, *, now: datetime | None = None) -> tuple[BudgetReservation, ...]:
        stamp = now or utc_now()
        return tuple(
            r
            for r in self.list_reservations()
            if r.status in ACTIVE_RESERVATION_STATUSES and r.expires_at > stamp
        )

    def _row(self, scope_ref: str) -> dict[str, Decimal]:
        if scope_ref not in self._ledger:
            self._ledger[scope_ref] = {
                "reserved": Decimal("0"),
                "committed": Decimal("0"),
                "spent": Decimal("0"),
            }
        return self._ledger[scope_ref]

    def add_reserved(self, scope_ref: str, amount: Decimal) -> None:
        with self._lock:
            row = self._row(scope_ref)
            row["reserved"] = row["reserved"] + Decimal(str(amount))

    def release_reserved(self, scope_ref: str, amount: Decimal) -> None:
        with self._lock:
            row = self._row(scope_ref)
            next_val = row["reserved"] - Decimal(str(amount))
            if next_val < 0:
                next_val = Decimal("0")
            row["reserved"] = next_val

    def add_spent(self, scope_ref: str, amount: Decimal) -> None:
        with self._lock:
            row = self._row(scope_ref)
            row["spent"] = row["spent"] + Decimal(str(amount))

    def get_totals(self, scope_ref: str) -> tuple[Decimal, Decimal, Decimal]:
        with self._lock:
            row = self._row(scope_ref)
            return row["reserved"], row["committed"], row["spent"]


class SqliteBudgetStore(BudgetStore):
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(DDL)
                conn.execute(
                    "INSERT OR REPLACE INTO finops_budget_schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
            except Exception as exc:
                raise BudgetPersistenceUnavailableError() from exc

    def begin_reserve_transaction(self):
        return _SqliteTx(self)

    def close(self) -> None:
        with self._lock:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None

    def _row_to_reservation(self, row: sqlite3.Row) -> BudgetReservation:
        refs = tuple(json.loads(row["scope_refs_json"] or "[]"))
        return BudgetReservation(
            reservation_id=row["reservation_id"],
            scope_refs=refs,
            task_id=row["task_id"],
            agent_id=row["agent_id"],
            provider=row["provider"],
            model=row["model"],
            estimated_cost=_dec(row["estimated_cost"]),
            currency=row["currency"],
            status=row["status"],
            created_at=_dt_from_db(row["created_at"]),
            expires_at=_dt_from_db(row["expires_at"]),
            committed_at=_dt_from_db(row["committed_at"]),
            released_at=_dt_from_db(row["released_at"]),
            actual_cost=_dec(row["actual_cost"]) if row["actual_cost"] else None,
            usage_record_key=row["usage_record_key"],
            metadata_safe=json.loads(row["metadata_json"] or "{}"),
            version=int(row["version"]),
        )

    def insert_reservation(self, reservation: BudgetReservation) -> BudgetReservation:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO finops_budget_reservations(
                        reservation_id, scope_refs_json, task_id, agent_id, provider, model,
                        estimated_cost, currency, status, created_at, expires_at,
                        committed_at, released_at, actual_cost, usage_record_key,
                        metadata_json, version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        reservation.reservation_id,
                        json.dumps(list(reservation.scope_refs)),
                        reservation.task_id,
                        reservation.agent_id,
                        reservation.provider,
                        reservation.model,
                        str(reservation.estimated_cost),
                        reservation.currency,
                        reservation.status,
                        _dt_to_db(reservation.created_at),
                        _dt_to_db(reservation.expires_at),
                        _dt_to_db(reservation.committed_at),
                        _dt_to_db(reservation.released_at),
                        str(reservation.actual_cost)
                        if reservation.actual_cost is not None
                        else None,
                        reservation.usage_record_key,
                        _json_dumps(dict(reservation.metadata_safe)),
                        reservation.version,
                    ),
                )
                return reservation
            except sqlite3.IntegrityError as exc:
                raise BudgetReservationConflictError("reservation_exists") from exc
            except sqlite3.Error as exc:
                raise BudgetPersistenceUnavailableError() from exc

    def get_reservation(self, reservation_id: str) -> BudgetReservation | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM finops_budget_reservations WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            return self._row_to_reservation(row) if row else None

    def update_reservation(
        self, reservation: BudgetReservation, *, expected_version: int | None = None
    ) -> BudgetReservation:
        with self._lock:
            conn = self._connect()
            current = self.get_reservation(reservation.reservation_id)
            if current is None:
                raise BudgetReservationConflictError("reservation_not_found")
            if expected_version is not None and current.version != expected_version:
                raise BudgetReservationConflictError("reservation_version_conflict")
            new_version = current.version + 1
            try:
                conn.execute(
                    """
                    UPDATE finops_budget_reservations SET
                        status=?, committed_at=?, released_at=?, actual_cost=?,
                        usage_record_key=?, metadata_json=?, version=?
                    WHERE reservation_id=? AND version=?
                    """,
                    (
                        reservation.status,
                        _dt_to_db(reservation.committed_at),
                        _dt_to_db(reservation.released_at),
                        str(reservation.actual_cost)
                        if reservation.actual_cost is not None
                        else None,
                        reservation.usage_record_key,
                        _json_dumps(dict(reservation.metadata_safe)),
                        new_version,
                        reservation.reservation_id,
                        current.version,
                    ),
                )
                if conn.total_changes == 0:
                    raise BudgetReservationConflictError("reservation_version_conflict")
            except BudgetReservationConflictError:
                raise
            except sqlite3.Error as exc:
                raise BudgetPersistenceUnavailableError() from exc
            updated = self.get_reservation(reservation.reservation_id)
            assert updated is not None
            return updated

    def list_reservations(
        self, *, status: str | None = None, task_id: str | None = None
    ) -> tuple[BudgetReservation, ...]:
        with self._lock:
            conn = self._connect()
            sql = "SELECT * FROM finops_budget_reservations WHERE 1=1"
            params: list = []
            if status is not None:
                sql += " AND status=?"
                params.append(status)
            if task_id is not None:
                sql += " AND task_id=?"
                params.append(task_id)
            sql += " ORDER BY reservation_id"
            rows = conn.execute(sql, params).fetchall()
            return tuple(self._row_to_reservation(r) for r in rows)

    def list_active(self, *, now: datetime | None = None) -> tuple[BudgetReservation, ...]:
        stamp = now or utc_now()
        return tuple(
            r
            for r in self.list_reservations()
            if r.status in ACTIVE_RESERVATION_STATUSES and r.expires_at > stamp
        )

    def _ensure_ledger(self, conn: sqlite3.Connection, scope_ref: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO finops_budget_ledger(scope_ref, reserved, committed, spent, version)
            VALUES (?, '0', '0', '0', 1)
            """,
            (scope_ref,),
        )

    def add_reserved(self, scope_ref: str, amount: Decimal) -> None:
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_ledger(conn, scope_ref)
                row = conn.execute(
                    "SELECT reserved FROM finops_budget_ledger WHERE scope_ref=?",
                    (scope_ref,),
                ).fetchone()
                nxt = _dec(row["reserved"]) + Decimal(str(amount))
                conn.execute(
                    "UPDATE finops_budget_ledger SET reserved=?, version=version+1 WHERE scope_ref=?",
                    (str(nxt), scope_ref),
                )
            except sqlite3.Error as exc:
                raise BudgetPersistenceUnavailableError() from exc

    def release_reserved(self, scope_ref: str, amount: Decimal) -> None:
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_ledger(conn, scope_ref)
                row = conn.execute(
                    "SELECT reserved, committed, spent, version FROM finops_budget_ledger WHERE scope_ref=?",
                    (scope_ref,),
                ).fetchone()
                current = _dec(row["reserved"])
                nxt = current - Decimal(str(amount))
                if nxt < 0:
                    nxt = Decimal("0")
                conn.execute(
                    "UPDATE finops_budget_ledger SET reserved=?, version=version+1 WHERE scope_ref=?",
                    (str(nxt), scope_ref),
                )
            except sqlite3.Error as exc:
                raise BudgetPersistenceUnavailableError() from exc

    def add_spent(self, scope_ref: str, amount: Decimal) -> None:
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_ledger(conn, scope_ref)
                row = conn.execute(
                    "SELECT spent FROM finops_budget_ledger WHERE scope_ref=?",
                    (scope_ref,),
                ).fetchone()
                nxt = _dec(row["spent"]) + Decimal(str(amount))
                conn.execute(
                    "UPDATE finops_budget_ledger SET spent=?, version=version+1 WHERE scope_ref=?",
                    (str(nxt), scope_ref),
                )
            except sqlite3.Error as exc:
                raise BudgetPersistenceUnavailableError() from exc

    def get_totals(self, scope_ref: str) -> tuple[Decimal, Decimal, Decimal]:
        with self._lock:
            conn = self._connect()
            self._ensure_ledger(conn, scope_ref)
            row = conn.execute(
                "SELECT reserved, committed, spent FROM finops_budget_ledger WHERE scope_ref=?",
                (scope_ref,),
            ).fetchone()
            return _dec(row["reserved"]), _dec(row["committed"]), _dec(row["spent"])


class _SqliteTx:
    def __init__(self, store: SqliteBudgetStore):
        self.store = store

    def __enter__(self):
        self.store._lock.acquire()
        conn = self.store._connect()
        conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        conn = self.store._connect()
        try:
            if exc_type is None:
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
        finally:
            self.store._lock.release()
        return False
