"""Production activation durable store."""

from __future__ import annotations

import json
import sqlite3
import threading

from production_activation.models import (
    ActivationAttempt,
    ActivationAuthorization,
    FinalProductionCandidate,
    GoLivePlan,
    ProductionActivationEvidence,
)


def _j(value) -> str:
    return json.dumps(value, default=str)


class SqliteProductionActivationStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    def _conn(self):
        return self._connection

    def _init_schema(self) -> None:
        with self._lock:
            self._conn().executescript(
                """
                CREATE TABLE IF NOT EXISTS pa_candidates(
                    candidate_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pa_plans(
                    plan_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pa_authorizations(
                    authorization_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pa_attempts(
                    attempt_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pa_evidence(
                    evidence_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pa_state(
                    key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pa_audit(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT,
                    payload_json TEXT NOT NULL);
                """
            )

    def save_candidate(self, candidate: FinalProductionCandidate) -> FinalProductionCandidate:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_candidates(candidate_id, payload_json) VALUES (?, ?)",
                (candidate.candidate_id, _j(candidate.as_dict())),
            )
            self._conn().commit()
        return candidate

    def get_candidate(self, candidate_id: str) -> FinalProductionCandidate | None:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM pa_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        if not row:
            return None
        return FinalProductionCandidate(**json.loads(row["payload_json"]))

    def save_plan(self, plan: GoLivePlan) -> GoLivePlan:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_plans(plan_id, candidate_id, payload_json) VALUES (?, ?, ?)",
                (plan.plan_id, plan.candidate_id, _j(plan.as_dict())),
            )
            self._conn().commit()
        return plan

    def get_plan(self, plan_id: str) -> GoLivePlan | None:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM pa_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["payload_json"])
        data["launch_required_providers"] = tuple(data.get("launch_required_providers") or [])
        data["smoke_plan"] = tuple(data.get("smoke_plan") or [])
        data["abort_conditions"] = tuple(data.get("abort_conditions") or [])
        data["rollback_conditions"] = tuple(data.get("rollback_conditions") or [])
        return GoLivePlan(**data)

    def save_authorization(self, auth: ActivationAuthorization) -> ActivationAuthorization:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_authorizations(authorization_id, payload_json) VALUES (?, ?)",
                (auth.authorization_id, _j(auth.as_dict())),
            )
            self._conn().commit()
        return auth

    def get_authorization(self, authorization_id: str) -> ActivationAuthorization | None:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM pa_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone()
        if not row:
            return None
        return ActivationAuthorization(**json.loads(row["payload_json"]))

    def save_attempt(self, attempt: ActivationAttempt) -> ActivationAttempt:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_attempts(attempt_id, candidate_id, payload_json) VALUES (?, ?, ?)",
                (attempt.attempt_id, attempt.candidate_id, _j(attempt.as_dict())),
            )
            self._conn().commit()
        return attempt

    def get_attempt_by_idempotency(self, candidate_id: str, idempotency_key: str) -> ActivationAttempt | None:
        with self._lock:
            rows = self._conn().execute(
                "SELECT payload_json FROM pa_attempts WHERE candidate_id=?",
                (candidate_id,),
            ).fetchall()
        for row in rows:
            data = json.loads(row["payload_json"])
            if data.get("idempotency_key") == idempotency_key:
                return ActivationAttempt(**data)
        return None

    def save_evidence(self, evidence: ProductionActivationEvidence) -> ProductionActivationEvidence:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_evidence(evidence_id, candidate_id, payload_json) VALUES (?, ?, ?)",
                (evidence.evidence_id, evidence.candidate_id, _j(evidence.as_dict())),
            )
            self._conn().commit()
        return evidence

    def list_evidence(self, candidate_id: str) -> list[ProductionActivationEvidence]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT payload_json FROM pa_evidence WHERE candidate_id=? ORDER BY evidence_id",
                (candidate_id,),
            ).fetchall()
        return [ProductionActivationEvidence(**json.loads(r["payload_json"])) for r in rows]

    def set_activation_state(self, state: str, *, candidate_id: str, extra: dict | None = None) -> None:
        payload = {"state": state, "candidate_id": candidate_id, "go_live_active": state == "PRODUCTION_ACTIVE"}
        if extra:
            payload.update(extra)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_state(key, payload_json) VALUES (?, ?)",
                ("activation_state", _j(payload)),
            )
            self._conn().commit()

    def get_activation_state(self) -> dict:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM pa_state WHERE key=?", ("activation_state",)).fetchone()
        if not row:
            return {"state": "GO_LIVE_ELIGIBLE", "candidate_id": "", "go_live_active": False}
        data = json.loads(row["payload_json"])
        data.setdefault("go_live_active", data.get("state") == "PRODUCTION_ACTIVE")
        return data

    def save_go_live_policy(self, policy) -> object:
        from production_activation.policy import GoLivePolicy

        if not isinstance(policy, GoLivePolicy):
            raise TypeError("GoLivePolicy required")
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_state(key, payload_json) VALUES (?, ?)",
                (f"go_live_policy:{policy.policy_id}", _j(policy.as_dict())),
            )
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_state(key, payload_json) VALUES (?, ?)",
                ("go_live_policy_latest", _j({"policy_id": policy.policy_id})),
            )
            self._conn().commit()
        return policy

    def latest_go_live_policy(self):
        from production_activation.policy import GoLivePolicy

        with self._lock:
            latest = self._conn().execute("SELECT payload_json FROM pa_state WHERE key=?", ("go_live_policy_latest",)).fetchone()
            if not latest:
                return None
            pid = json.loads(latest["payload_json"]).get("policy_id")
            row = self._conn().execute("SELECT payload_json FROM pa_state WHERE key=?", (f"go_live_policy:{pid}",)).fetchone()
        if not row:
            return None
        return GoLivePolicy.from_dict(json.loads(row["payload_json"]))

    def append_audit(self, event: dict) -> dict:
        with self._lock:
            self._conn().execute(
                "INSERT INTO pa_audit(candidate_id, payload_json) VALUES (?, ?)",
                (event.get("candidate_id"), _j(event)),
            )
            self._conn().commit()
        return event

    def list_audit(self, *, candidate_id: str | None = None) -> list[dict]:
        with self._lock:
            if candidate_id:
                rows = self._conn().execute(
                    "SELECT payload_json FROM pa_audit WHERE candidate_id=? ORDER BY event_id",
                    (candidate_id,),
                ).fetchall()
            else:
                rows = self._conn().execute("SELECT payload_json FROM pa_audit ORDER BY event_id").fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
