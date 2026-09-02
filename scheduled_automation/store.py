"""Schedule definition and occurrence persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scheduled_automation.models import ScheduleDefinition, ScheduleOccurrence


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScheduleAutomationStore:
    def create_schedule(self, definition: ScheduleDefinition) -> ScheduleDefinition:
        raise NotImplementedError

    def get_schedule(self, *, tenant_id: str, schedule_id: str) -> ScheduleDefinition | None:
        raise NotImplementedError

    def list_schedules(self, *, tenant_id: str, limit: int = 50, offset: int = 0) -> list[ScheduleDefinition]:
        raise NotImplementedError

    def update_schedule(self, definition: ScheduleDefinition, *, expected_version: int) -> ScheduleDefinition:
        raise NotImplementedError

    def save_occurrence(self, occ: ScheduleOccurrence) -> ScheduleOccurrence:
        raise NotImplementedError

    def get_occurrence(self, *, tenant_id: str, occurrence_id: str) -> ScheduleOccurrence | None:
        raise NotImplementedError

    def list_occurrences(self, *, tenant_id: str, schedule_id: str, limit: int = 50) -> list[ScheduleOccurrence]:
        raise NotImplementedError

    def claim_occurrence(self, *, tenant_id: str, occurrence_id: str, now: str) -> bool:
        raise NotImplementedError

    def list_due_schedules(self, *, tenant_id: str | None, now: str) -> list[ScheduleDefinition]:
        raise NotImplementedError

    def append_audit(self, *, tenant_id: str, event_type: str, schedule_id: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    def list_audit(self, *, tenant_id: str, schedule_id: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError


class InMemoryScheduleAutomationStore(ScheduleAutomationStore):
    def __init__(self):
        self._schedules: dict[tuple[str, str], ScheduleDefinition] = {}
        self._occurrences: dict[tuple[str, str], ScheduleOccurrence] = {}
        self._audit: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def create_schedule(self, definition: ScheduleDefinition) -> ScheduleDefinition:
        with self._lock:
            key = (definition.tenant_id, definition.schedule_id)
            if key in self._schedules:
                raise KeyError("schedule_exists")
            self._schedules[key] = definition
            return definition

    def get_schedule(self, *, tenant_id: str, schedule_id: str) -> ScheduleDefinition | None:
        return self._schedules.get((tenant_id, schedule_id))

    def list_schedules(self, *, tenant_id: str, limit: int = 50, offset: int = 0) -> list[ScheduleDefinition]:
        items = [s for (t, _), s in self._schedules.items() if t == tenant_id and not s.archived]
        return items[offset : offset + limit]

    def update_schedule(self, definition: ScheduleDefinition, *, expected_version: int) -> ScheduleDefinition:
        with self._lock:
            key = (definition.tenant_id, definition.schedule_id)
            current = self._schedules.get(key)
            if current is None:
                raise KeyError("not_found")
            if current.version != expected_version:
                raise ValueError("stale_version")
            self._schedules[key] = definition
            return definition

    def save_occurrence(self, occ: ScheduleOccurrence) -> ScheduleOccurrence:
        with self._lock:
            self._occurrences[(occ.tenant_id, occ.occurrence_id)] = occ
            return occ

    def get_occurrence(self, *, tenant_id: str, occurrence_id: str) -> ScheduleOccurrence | None:
        return self._occurrences.get((tenant_id, occurrence_id))

    def list_occurrences(self, *, tenant_id: str, schedule_id: str, limit: int = 50) -> list[ScheduleOccurrence]:
        items = [o for (t, _), o in self._occurrences.items() if t == tenant_id and o.schedule_id == schedule_id]
        return items[:limit]

    def claim_occurrence(self, *, tenant_id: str, occurrence_id: str, now: str) -> bool:
        with self._lock:
            occ = self._occurrences.get((tenant_id, occurrence_id))
            if occ is None or occ.status != "PENDING":
                return False
            self._occurrences[(tenant_id, occurrence_id)] = ScheduleOccurrence(
                **{**occ.__dict__, "status": "CLAIMED", "claimed_at": now}
            )
            return True

    def list_due_schedules(self, *, tenant_id: str | None, now: str) -> list[ScheduleDefinition]:
        due = []
        for (t, _), s in self._schedules.items():
            if tenant_id and t != tenant_id:
                continue
            if not s.enabled or s.paused or s.archived:
                continue
            if s.next_run_at and s.next_run_at <= now:
                due.append(s)
        return due

    def append_audit(self, *, tenant_id: str, event_type: str, schedule_id: str, payload: dict[str, Any]) -> None:
        self._audit.append({"tenant_id": tenant_id, "event_type": event_type, "schedule_id": schedule_id, "payload": payload, "at": _utc_iso()})

    def list_audit(self, *, tenant_id: str, schedule_id: str | None = None) -> list[dict[str, Any]]:
        out = [a for a in self._audit if a["tenant_id"] == tenant_id]
        if schedule_id:
            out = [a for a in out if a["schedule_id"] == schedule_id]
        return out


class SqliteScheduleAutomationStore(ScheduleAutomationStore):
  def __init__(self, db_path: str):
    self._path = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    self._init_db()

  def _conn(self):
    conn = sqlite3.connect(self._path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

  def _init_db(self):
    with self._conn() as conn:
      conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schedules (
          tenant_id TEXT NOT NULL,
          schedule_id TEXT NOT NULL,
          body TEXT NOT NULL,
          version INTEGER NOT NULL,
          next_run_at TEXT,
          archived INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (tenant_id, schedule_id)
        );
        CREATE TABLE IF NOT EXISTS occurrences (
          tenant_id TEXT NOT NULL,
          occurrence_id TEXT NOT NULL,
          schedule_id TEXT NOT NULL,
          body TEXT NOT NULL,
          status TEXT NOT NULL,
          PRIMARY KEY (tenant_id, occurrence_id)
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id TEXT NOT NULL,
          schedule_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload TEXT NOT NULL,
          at TEXT NOT NULL
        );
        """
      )

  def create_schedule(self, definition: ScheduleDefinition) -> ScheduleDefinition:
    with self._conn() as conn:
      conn.execute(
        "INSERT INTO schedules (tenant_id, schedule_id, body, version, next_run_at, archived) VALUES (?,?,?,?,?,?)",
        (definition.tenant_id, definition.schedule_id, json.dumps(definition.__dict__), definition.version, definition.next_run_at, int(definition.archived)),
      )
    return definition

  def get_schedule(self, *, tenant_id: str, schedule_id: str) -> ScheduleDefinition | None:
    with self._conn() as conn:
      row = conn.execute("SELECT body FROM schedules WHERE tenant_id=? AND schedule_id=?", (tenant_id, schedule_id)).fetchone()
    if not row:
      return None
    data = json.loads(row["body"])
    return ScheduleDefinition(**data)

  def list_schedules(self, *, tenant_id: str, limit: int = 50, offset: int = 0) -> list[ScheduleDefinition]:
    with self._conn() as conn:
      rows = conn.execute(
        "SELECT body FROM schedules WHERE tenant_id=? AND archived=0 ORDER BY schedule_id LIMIT ? OFFSET ?",
        (tenant_id, limit, offset),
      ).fetchall()
    return [ScheduleDefinition(**json.loads(r["body"])) for r in rows]

  def update_schedule(self, definition: ScheduleDefinition, *, expected_version: int) -> ScheduleDefinition:
    with self._conn() as conn:
      cur = conn.execute(
        "UPDATE schedules SET body=?, version=?, next_run_at=?, archived=? WHERE tenant_id=? AND schedule_id=? AND version=?",
        (
          json.dumps(definition.__dict__),
          definition.version,
          definition.next_run_at,
          int(definition.archived),
          definition.tenant_id,
          definition.schedule_id,
          expected_version,
        ),
      )
      if cur.rowcount != 1:
        raise ValueError("stale_version")
    return definition

  def save_occurrence(self, occ: ScheduleOccurrence) -> ScheduleOccurrence:
    with self._conn() as conn:
      conn.execute(
        "INSERT OR REPLACE INTO occurrences (tenant_id, occurrence_id, schedule_id, body, status) VALUES (?,?,?,?,?)",
        (occ.tenant_id, occ.occurrence_id, occ.schedule_id, json.dumps(occ.__dict__), occ.status),
      )
    return occ

  def get_occurrence(self, *, tenant_id: str, occurrence_id: str) -> ScheduleOccurrence | None:
    with self._conn() as conn:
      row = conn.execute("SELECT body FROM occurrences WHERE tenant_id=? AND occurrence_id=?", (tenant_id, occurrence_id)).fetchone()
    return ScheduleOccurrence(**json.loads(row["body"])) if row else None

  def list_occurrences(self, *, tenant_id: str, schedule_id: str, limit: int = 50) -> list[ScheduleOccurrence]:
    with self._conn() as conn:
      rows = conn.execute(
        "SELECT body FROM occurrences WHERE tenant_id=? AND schedule_id=? ORDER BY occurrence_id DESC LIMIT ?",
        (tenant_id, schedule_id, limit),
      ).fetchall()
    return [ScheduleOccurrence(**json.loads(r["body"])) for r in rows]

  def claim_occurrence(self, *, tenant_id: str, occurrence_id: str, now: str) -> bool:
    occ = self.get_occurrence(tenant_id=tenant_id, occurrence_id=occurrence_id)
    if occ is None or occ.status != "PENDING":
      return False
    occ = ScheduleOccurrence(**{**occ.__dict__, "status": "CLAIMED", "claimed_at": now})
    self.save_occurrence(occ)
    return True

  def list_due_schedules(self, *, tenant_id: str | None, now: str) -> list[ScheduleDefinition]:
    with self._conn() as conn:
      if tenant_id:
        rows = conn.execute(
          "SELECT body FROM schedules WHERE tenant_id=? AND archived=0 AND next_run_at IS NOT NULL AND next_run_at <= ?",
          (tenant_id, now),
        ).fetchall()
      else:
        rows = conn.execute(
          "SELECT body FROM schedules WHERE archived=0 AND next_run_at IS NOT NULL AND next_run_at <= ?",
          (now,),
        ).fetchall()
    out = []
    for r in rows:
      s = ScheduleDefinition(**json.loads(r["body"]))
      if s.enabled and not s.paused:
        out.append(s)
    return out

  def append_audit(self, *, tenant_id: str, event_type: str, schedule_id: str, payload: dict[str, Any]) -> None:
    with self._conn() as conn:
      conn.execute(
        "INSERT INTO audit (tenant_id, schedule_id, event_type, payload, at) VALUES (?,?,?,?,?)",
        (tenant_id, schedule_id, event_type, json.dumps(payload), _utc_iso()),
      )

  def list_audit(self, *, tenant_id: str, schedule_id: str | None = None) -> list[dict[str, Any]]:
    with self._conn() as conn:
      if schedule_id:
        rows = conn.execute("SELECT * FROM audit WHERE tenant_id=? AND schedule_id=? ORDER BY id", (tenant_id, schedule_id)).fetchall()
      else:
        rows = conn.execute("SELECT * FROM audit WHERE tenant_id=? ORDER BY id", (tenant_id,)).fetchall()
    return [dict(r) for r in rows]
