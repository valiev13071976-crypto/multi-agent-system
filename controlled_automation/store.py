"""Controlled automation persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from controlled_automation.models import AutomationRun, ControlledAutomationDefinition, PolicyEnvelope


class ControlledAutomationStore:
    def create(self, definition: ControlledAutomationDefinition) -> ControlledAutomationDefinition:
        raise NotImplementedError

    def get(self, *, tenant_id: str, automation_id: str) -> ControlledAutomationDefinition | None:
        raise NotImplementedError

    def list(self, *, tenant_id: str, limit: int = 50, offset: int = 0) -> list[ControlledAutomationDefinition]:
        raise NotImplementedError

    def update(self, definition: ControlledAutomationDefinition, *, expected_version: int) -> ControlledAutomationDefinition:
        raise NotImplementedError

    def save_run(self, run: AutomationRun) -> AutomationRun:
        raise NotImplementedError

    def get_run(self, *, tenant_id: str, run_id: str) -> AutomationRun | None:
        raise NotImplementedError

    def list_runs(self, *, tenant_id: str, automation_id: str, limit: int = 50) -> list[AutomationRun]:
        raise NotImplementedError

    def append_audit(self, *, tenant_id: str, automation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError


def _def_to_dict(d: ControlledAutomationDefinition) -> dict:
    data = dict(d.__dict__)
    data["policy"] = asdict(d.policy)
    data["actions"] = list(d.actions)
    return data


def _def_from_dict(data: dict) -> ControlledAutomationDefinition:
    policy = PolicyEnvelope(**data.pop("policy"))
    actions = tuple(data.pop("actions", ()))
    data["required_capabilities"] = tuple(data.get("required_capabilities") or ())
    return ControlledAutomationDefinition(policy=policy, actions=actions, **data)


def _run_to_dict(r: AutomationRun) -> dict:
    data = dict(r.__dict__)
    data["actions_planned"] = list(r.actions_planned)
    data["actions_executed"] = list(r.actions_executed)
    return data


def _run_from_dict(data: dict) -> AutomationRun:
    data["actions_planned"] = tuple(data.pop("actions_planned", ()))
    data["actions_executed"] = tuple(data.pop("actions_executed", ()))
    return AutomationRun(**data)


class InMemoryControlledAutomationStore(ControlledAutomationStore):
    def __init__(self):
        self._defs: dict[tuple[str, str], ControlledAutomationDefinition] = {}
        self._runs: dict[tuple[str, str], AutomationRun] = {}
        self._audit: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def create(self, definition: ControlledAutomationDefinition) -> ControlledAutomationDefinition:
        with self._lock:
            key = (definition.tenant_id, definition.automation_id)
            if key in self._defs:
                raise KeyError("exists")
            self._defs[key] = definition
            return definition

    def get(self, *, tenant_id: str, automation_id: str) -> ControlledAutomationDefinition | None:
        return self._defs.get((tenant_id, automation_id))

    def list(self, *, tenant_id: str, limit: int = 50, offset: int = 0) -> list[ControlledAutomationDefinition]:
        items = [d for (t, _), d in self._defs.items() if t == tenant_id and not d.archived]
        return items[offset : offset + limit]

    def update(self, definition: ControlledAutomationDefinition, *, expected_version: int) -> ControlledAutomationDefinition:
        with self._lock:
            key = (definition.tenant_id, definition.automation_id)
            cur = self._defs.get(key)
            if cur is None:
                raise KeyError("not_found")
            if cur.version != expected_version:
                raise ValueError("stale_version")
            self._defs[key] = definition
            return definition

    def save_run(self, run: AutomationRun) -> AutomationRun:
        with self._lock:
            self._runs[(run.tenant_id, run.run_id)] = run
            return run

    def get_run(self, *, tenant_id: str, run_id: str) -> AutomationRun | None:
        return self._runs.get((tenant_id, run_id))

    def list_runs(self, *, tenant_id: str, automation_id: str, limit: int = 50) -> list[AutomationRun]:
        items = [r for (t, _), r in self._runs.items() if t == tenant_id and r.automation_id == automation_id]
        return items[:limit]

    def append_audit(self, *, tenant_id: str, automation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._audit.append({"tenant_id": tenant_id, "automation_id": automation_id, "event_type": event_type, "payload": payload})


class SqliteControlledAutomationStore(ControlledAutomationStore):
    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS automations (tenant_id TEXT, automation_id TEXT, body TEXT, version INT, PRIMARY KEY (tenant_id, automation_id));
                CREATE TABLE IF NOT EXISTS runs (tenant_id TEXT, run_id TEXT, body TEXT, PRIMARY KEY (tenant_id, run_id));
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, automation_id TEXT, event_type TEXT, payload TEXT);
                """
            )

    def _conn(self):
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, definition: ControlledAutomationDefinition) -> ControlledAutomationDefinition:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO automations (tenant_id, automation_id, body, version) VALUES (?,?,?,?)",
                (definition.tenant_id, definition.automation_id, json.dumps(_def_to_dict(definition)), definition.version),
            )
        return definition

    def get(self, *, tenant_id: str, automation_id: str) -> ControlledAutomationDefinition | None:
        with self._conn() as conn:
            row = conn.execute("SELECT body FROM automations WHERE tenant_id=? AND automation_id=?", (tenant_id, automation_id)).fetchone()
        return _def_from_dict(json.loads(row["body"])) if row else None

    def list(self, *, tenant_id: str, limit: int = 50, offset: int = 0) -> list[ControlledAutomationDefinition]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT body FROM automations WHERE tenant_id=? LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
        return [_def_from_dict(json.loads(r["body"])) for r in rows]

    def update(self, definition: ControlledAutomationDefinition, *, expected_version: int) -> ControlledAutomationDefinition:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE automations SET body=?, version=? WHERE tenant_id=? AND automation_id=? AND version=?",
                (json.dumps(_def_to_dict(definition)), definition.version, definition.tenant_id, definition.automation_id, expected_version),
            )
            if cur.rowcount != 1:
                raise ValueError("stale_version")
        return definition

    def save_run(self, run: AutomationRun) -> AutomationRun:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (tenant_id, run_id, body) VALUES (?,?,?)",
                (run.tenant_id, run.run_id, json.dumps(_run_to_dict(run))),
            )
        return run

    def get_run(self, *, tenant_id: str, run_id: str) -> AutomationRun | None:
        with self._conn() as conn:
            row = conn.execute("SELECT body FROM runs WHERE tenant_id=? AND run_id=?", (tenant_id, run_id)).fetchone()
        return _run_from_dict(json.loads(row["body"])) if row else None

    def list_runs(self, *, tenant_id: str, automation_id: str, limit: int = 50) -> list[AutomationRun]:
        with self._conn() as conn:
            rows = conn.execute("SELECT body FROM runs WHERE tenant_id=?", (tenant_id,)).fetchall()
        out = [_run_from_dict(json.loads(r["body"])) for r in rows if json.loads(r["body"]).get("automation_id") == automation_id]
        return out[:limit]

    def append_audit(self, *, tenant_id: str, automation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit (tenant_id, automation_id, event_type, payload) VALUES (?,?,?,?)",
                (tenant_id, automation_id, event_type, json.dumps(payload)),
            )
