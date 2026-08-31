"""Production activation durable store."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from production_activation.errors import (
    ACTIVATION_CONFLICT,
    ACTIVATION_FAILED,
    AUTHORIZATION_DENIED,
    AUTHORIZATION_REPLAY,
    AUTHORIZATION_STALE,
    ProductionActivationError,
)
from production_activation.models import (
    ActivationAttempt,
    ActivationAuthorization,
    ActivationState,
    FinalProductionCandidate,
    GoLivePlan,
    ProductionActivationEvidence,
)


def _j(value) -> str:
    return json.dumps(value, default=str)


def _parse_expiry(expires_at: str) -> datetime:
    expires = datetime.fromisoformat(expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires


class SqliteProductionActivationStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:":
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        if path != ":memory:":
            try:
                self._connection.execute("PRAGMA journal_mode=WAL;")
            except sqlite3.Error:
                pass
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
                CREATE TABLE IF NOT EXISTS pa_idempotency(
                    candidate_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    PRIMARY KEY (candidate_id, idempotency_key));
                CREATE TABLE IF NOT EXISTS pa_evidence(
                    evidence_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pa_hypercare(
                    candidate_id TEXT PRIMARY KEY,
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
        data = json.loads(row["payload_json"])
        # Backward-compatible defaults for pre-patch auth rows
        data.setdefault("consumed", False)
        data.setdefault("consumed_at", "")
        data.setdefault("attempt_id", "")
        data.setdefault("candidate_id", "")
        data.setdefault("plan_id", "")
        data.setdefault("release_identity", "")
        return ActivationAuthorization(**data)

    def consume_authorization(
        self,
        authorization_id: str,
        *,
        attempt_id: str,
        operator_ref: str,
        candidate_id: str,
        plan_id: str,
        release_identity: str = "",
    ) -> ActivationAuthorization:
        """Atomic fail-closed consume. Concurrent double-consume raises AUTHORIZATION_REPLAY."""
        with self._lock:
            self._conn().execute("BEGIN IMMEDIATE")
            try:
                row = self._conn().execute(
                    "SELECT payload_json FROM pa_authorizations WHERE authorization_id=?",
                    (authorization_id,),
                ).fetchone()
                if not row:
                    raise ProductionActivationError("target_not_found")
                data = json.loads(row["payload_json"])
                data.setdefault("consumed", False)
                data.setdefault("consumed_at", "")
                data.setdefault("attempt_id", "")
                data.setdefault("candidate_id", "")
                data.setdefault("plan_id", "")
                data.setdefault("release_identity", "")
                auth = ActivationAuthorization(**data)
                if auth.consumed:
                    raise ProductionActivationError(AUTHORIZATION_REPLAY)
                if datetime.now(timezone.utc) > _parse_expiry(auth.expires_at):
                    raise ProductionActivationError(AUTHORIZATION_STALE)
                if auth.operator_ref != operator_ref:
                    raise ProductionActivationError(AUTHORIZATION_DENIED, details={"operator": "mismatch"})
                if auth.candidate_id and auth.candidate_id != candidate_id:
                    raise ProductionActivationError(AUTHORIZATION_DENIED, details={"candidate_id": "mismatch"})
                if auth.plan_id and auth.plan_id != plan_id:
                    raise ProductionActivationError(AUTHORIZATION_DENIED, details={"plan_id": "mismatch"})
                if auth.release_identity and release_identity and auth.release_identity != release_identity:
                    raise ProductionActivationError(AUTHORIZATION_DENIED, details={"release_identity": "mismatch"})
                consumed = ActivationAuthorization(
                    authorization_id=auth.authorization_id,
                    candidate_fingerprint=auth.candidate_fingerprint,
                    deployment_fingerprint=auth.deployment_fingerprint,
                    plan_fingerprint=auth.plan_fingerprint,
                    operator_ref=auth.operator_ref,
                    confirmation_token=auth.confirmation_token,
                    idempotency_key=auth.idempotency_key,
                    issued_at=auth.issued_at,
                    expires_at=auth.expires_at,
                    consumed=True,
                    consumed_at=datetime.now(timezone.utc).isoformat(),
                    attempt_id=attempt_id,
                    candidate_id=auth.candidate_id or candidate_id,
                    plan_id=auth.plan_id or plan_id,
                    release_identity=auth.release_identity or release_identity,
                )
                self._conn().execute(
                    "UPDATE pa_authorizations SET payload_json=? WHERE authorization_id=?",
                    (_j(consumed.as_dict()), authorization_id),
                )
                self._conn().commit()
                return consumed
            except Exception:
                self._conn().rollback()
                raise

    def save_attempt(self, attempt: ActivationAttempt) -> ActivationAttempt:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_attempts(attempt_id, candidate_id, payload_json) VALUES (?, ?, ?)",
                (attempt.attempt_id, attempt.candidate_id, _j(attempt.as_dict())),
            )
            if attempt.idempotency_key:
                self._conn().execute(
                    "INSERT OR REPLACE INTO pa_idempotency(candidate_id, idempotency_key, attempt_id) VALUES (?, ?, ?)",
                    (attempt.candidate_id, attempt.idempotency_key, attempt.attempt_id),
                )
            self._conn().commit()
        return attempt

    def get_attempt(self, attempt_id: str) -> ActivationAttempt | None:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM pa_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["payload_json"])
        data.setdefault("idempotency_key", "")
        data.setdefault("already_applied", False)
        return ActivationAttempt(**data)

    def get_attempt_by_idempotency(self, candidate_id: str, idempotency_key: str) -> ActivationAttempt | None:
        with self._lock:
            row = self._conn().execute(
                "SELECT attempt_id FROM pa_idempotency WHERE candidate_id=? AND idempotency_key=?",
                (candidate_id, idempotency_key),
            ).fetchone()
            if row:
                attempt_row = self._conn().execute(
                    "SELECT payload_json FROM pa_attempts WHERE attempt_id=?",
                    (row["attempt_id"],),
                ).fetchone()
                if attempt_row:
                    data = json.loads(attempt_row["payload_json"])
                    data.setdefault("idempotency_key", idempotency_key)
                    data.setdefault("already_applied", False)
                    return ActivationAttempt(**data)
            # Legacy rows: scan payload
            rows = self._conn().execute(
                "SELECT payload_json FROM pa_attempts WHERE candidate_id=?",
                (candidate_id,),
            ).fetchall()
        for row in rows:
            data = json.loads(row["payload_json"])
            if data.get("idempotency_key") == idempotency_key:
                data.setdefault("already_applied", False)
                return ActivationAttempt(**data)
        return None

    def begin_activation_attempt(
        self,
        *,
        candidate_id: str,
        plan_id: str,
        authorization_id: str,
        operator_ref: str,
        idempotency_key: str,
        release_identity: str = "",
    ) -> ActivationAttempt:
        """
        Durable reservation + auth consume under BEGIN IMMEDIATE.

        - Successful prior attempt → returns already_applied=True (no re-consume).
        - Non-terminal ACTIVATING → fail-closed.
        - Failed terminal → fail-closed.
        - New attempt → consumes authorization atomically and persists ACTIVATING.
        """
        with self._lock:
            self._conn().execute("BEGIN IMMEDIATE")
            try:
                existing = None
                idem_row = self._conn().execute(
                    "SELECT attempt_id FROM pa_idempotency WHERE candidate_id=? AND idempotency_key=?",
                    (candidate_id, idempotency_key),
                ).fetchone()
                if idem_row:
                    attempt_row = self._conn().execute(
                        "SELECT payload_json FROM pa_attempts WHERE attempt_id=?",
                        (idem_row["attempt_id"],),
                    ).fetchone()
                    if attempt_row:
                        data = json.loads(attempt_row["payload_json"])
                        data.setdefault("idempotency_key", idempotency_key)
                        data.setdefault("already_applied", False)
                        existing = ActivationAttempt(**data)

                if existing is not None:
                    if existing.state == ActivationState.PRODUCTION_ACTIVE.value:
                        existing.already_applied = True
                        self._conn().commit()
                        return existing
                    if existing.state == ActivationState.ACTIVATING.value:
                        raise ProductionActivationError(
                            ACTIVATION_CONFLICT,
                            details={"reason": "non_terminal_activating", "attempt_id": existing.attempt_id},
                        )
                    if existing.state == ActivationState.ACTIVATION_FAILED.value:
                        raise ProductionActivationError(
                            ACTIVATION_FAILED,
                            details={"reason": "prior_failed_attempt", "attempt_id": existing.attempt_id},
                        )
                    raise ProductionActivationError(
                        ACTIVATION_CONFLICT,
                        details={"reason": "idempotency_conflict", "state": existing.state, "attempt_id": existing.attempt_id},
                    )

                auth_row = self._conn().execute(
                    "SELECT payload_json FROM pa_authorizations WHERE authorization_id=?",
                    (authorization_id,),
                ).fetchone()
                if not auth_row:
                    raise ProductionActivationError("target_not_found")
                auth_data = json.loads(auth_row["payload_json"])
                auth_data.setdefault("consumed", False)
                auth_data.setdefault("consumed_at", "")
                auth_data.setdefault("attempt_id", "")
                auth_data.setdefault("candidate_id", "")
                auth_data.setdefault("plan_id", "")
                auth_data.setdefault("release_identity", "")
                auth = ActivationAuthorization(**auth_data)
                if auth.consumed:
                    raise ProductionActivationError(AUTHORIZATION_REPLAY)
                if datetime.now(timezone.utc) > _parse_expiry(auth.expires_at):
                    raise ProductionActivationError(AUTHORIZATION_STALE)
                if auth.operator_ref != operator_ref:
                    raise ProductionActivationError(AUTHORIZATION_DENIED, details={"operator": "mismatch"})
                if auth.candidate_id and auth.candidate_id != candidate_id:
                    raise ProductionActivationError(AUTHORIZATION_DENIED, details={"candidate_id": "mismatch"})
                if auth.plan_id and auth.plan_id != plan_id:
                    raise ProductionActivationError(AUTHORIZATION_DENIED, details={"plan_id": "mismatch"})
                if auth.release_identity and release_identity and auth.release_identity != release_identity:
                    raise ProductionActivationError(AUTHORIZATION_DENIED, details={"release_identity": "mismatch"})

                attempt = ActivationAttempt(
                    attempt_id=f"act-{uuid.uuid4().hex[:12]}",
                    candidate_id=candidate_id,
                    plan_id=plan_id,
                    authorization_id=authorization_id,
                    operator_ref=operator_ref,
                    state=ActivationState.ACTIVATING.value,
                    idempotency_key=idempotency_key,
                )
                consumed = ActivationAuthorization(
                    authorization_id=auth.authorization_id,
                    candidate_fingerprint=auth.candidate_fingerprint,
                    deployment_fingerprint=auth.deployment_fingerprint,
                    plan_fingerprint=auth.plan_fingerprint,
                    operator_ref=auth.operator_ref,
                    confirmation_token=auth.confirmation_token,
                    idempotency_key=auth.idempotency_key,
                    issued_at=auth.issued_at,
                    expires_at=auth.expires_at,
                    consumed=True,
                    consumed_at=datetime.now(timezone.utc).isoformat(),
                    attempt_id=attempt.attempt_id,
                    candidate_id=auth.candidate_id or candidate_id,
                    plan_id=auth.plan_id or plan_id,
                    release_identity=auth.release_identity or release_identity,
                )
                self._conn().execute(
                    "UPDATE pa_authorizations SET payload_json=? WHERE authorization_id=?",
                    (_j(consumed.as_dict()), authorization_id),
                )
                try:
                    self._conn().execute(
                        "INSERT INTO pa_attempts(attempt_id, candidate_id, payload_json) VALUES (?, ?, ?)",
                        (attempt.attempt_id, attempt.candidate_id, _j(attempt.as_dict())),
                    )
                    self._conn().execute(
                        "INSERT INTO pa_idempotency(candidate_id, idempotency_key, attempt_id) VALUES (?, ?, ?)",
                        (candidate_id, idempotency_key, attempt.attempt_id),
                    )
                except sqlite3.IntegrityError as exc:
                    # Concurrent winner already reserved this idempotency key
                    self._conn().rollback()
                    winner = self.get_attempt_by_idempotency(candidate_id, idempotency_key)
                    if winner and winner.state == ActivationState.PRODUCTION_ACTIVE.value:
                        winner.already_applied = True
                        return winner
                    if winner and winner.state == ActivationState.ACTIVATING.value:
                        raise ProductionActivationError(
                            ACTIVATION_CONFLICT,
                            details={"reason": "concurrent_activating", "attempt_id": winner.attempt_id},
                        ) from exc
                    raise ProductionActivationError(
                        ACTIVATION_CONFLICT,
                        details={"reason": "idempotency_race", "attempt_id": winner.attempt_id if winner else ""},
                    ) from exc
                self._conn().commit()
                return attempt
            except Exception:
                self._conn().rollback()
                raise

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

    def save_hypercare(self, session: dict) -> dict:
        candidate_id = str(session.get("candidate_id") or "")
        if not candidate_id:
            raise ProductionActivationError("target_not_found", details={"hypercare": "missing_candidate_id"})
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_hypercare(candidate_id, payload_json) VALUES (?, ?)",
                (candidate_id, _j(session)),
            )
            self._conn().commit()
        return session

    def get_hypercare(self, candidate_id: str) -> dict | None:
        with self._lock:
            row = self._conn().execute(
                "SELECT payload_json FROM pa_hypercare WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def set_activation_state(self, state: str, *, candidate_id: str, extra: dict | None = None) -> None:
        with self._lock:
            existing = {}
            row = self._conn().execute("SELECT payload_json FROM pa_state WHERE key=?", ("activation_state",)).fetchone()
            if row:
                existing = json.loads(row["payload_json"])
            payload = dict(existing)
            payload.update(
                {
                    "state": state,
                    "candidate_id": candidate_id or existing.get("candidate_id") or "",
                    "go_live_active": state == "PRODUCTION_ACTIVE",
                }
            )
            if state != "PRODUCTION_ACTIVE":
                payload["live_verified"] = False
            if extra:
                payload.update(extra)
            # Enforce ACTIVE != LIVE_VERIFIED unless explicitly accepted
            if not payload.get("go_live_active"):
                payload["live_verified"] = False
            self._conn().execute(
                "INSERT OR REPLACE INTO pa_state(key, payload_json) VALUES (?, ?)",
                ("activation_state", _j(payload)),
            )
            self._conn().commit()

    def get_activation_state(self) -> dict:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM pa_state WHERE key=?", ("activation_state",)).fetchone()
        if not row:
            return {"state": "GO_LIVE_ELIGIBLE", "candidate_id": "", "go_live_active": False, "live_verified": False}
        data = json.loads(row["payload_json"])
        data.setdefault("go_live_active", data.get("state") == "PRODUCTION_ACTIVE")
        data.setdefault("live_verified", False)
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
