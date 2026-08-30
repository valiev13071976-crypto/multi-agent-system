"""SQLite durable store for controlled launch."""

from __future__ import annotations

import json
import sqlite3
import threading

from controlled_launch.models import LaunchCandidate, LaunchEvidence, RolloutState, TrafficPolicy


def _j(value) -> str:
    return json.dumps(value, default=str)


class SqliteControlledLaunchStore:
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
                CREATE TABLE IF NOT EXISTS lc_candidates(
                    candidate_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lc_policies(
                    policy_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lc_rollout(
                    candidate_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lc_evidence(
                    evidence_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lc_audit(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lc_launch_policies(
                    policy_id TEXT PRIMARY KEY,
                    release_identity TEXT NOT NULL,
                    payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lc_launch_state(
                    key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL);
                """
            )

    def save_candidate(self, candidate: LaunchCandidate) -> LaunchCandidate:
        with self._lock:
            existing = self.get_candidate(candidate.candidate_id)
            if existing and existing.status != "DRAFT":
                for key in ("candidate_id", "commit_sha", "deployment_id", "rollback_target", "stage3_evidence_id"):
                    if getattr(existing, key) != getattr(candidate, key):
                        from controlled_launch.errors import CANDIDATE_IMMUTABLE, ControlledLaunchError

                        raise ControlledLaunchError(CANDIDATE_IMMUTABLE, details={"field": key})
            self._conn().execute(
                "INSERT OR REPLACE INTO lc_candidates(candidate_id, payload_json) VALUES (?, ?)",
                (candidate.candidate_id, _j(candidate.as_dict())),
            )
            self._conn().commit()
        return candidate

    def get_candidate(self, candidate_id: str) -> LaunchCandidate | None:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM lc_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["payload_json"])
        return LaunchCandidate(**data)

    def save_policy(self, policy: TrafficPolicy) -> TrafficPolicy:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO lc_policies(policy_id, candidate_id, payload_json) VALUES (?, ?, ?)",
                (policy.policy_id, policy.candidate_id, _j(policy.as_dict())),
            )
            self._conn().commit()
        return policy

    def get_policy(self, policy_id: str) -> TrafficPolicy | None:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM lc_policies WHERE policy_id=?", (policy_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["payload_json"])
        data["internal_tenants"] = frozenset(data.get("internal_tenants") or [])
        data["test_tenants"] = frozenset(data.get("test_tenants") or [])
        data["canary_tenants"] = frozenset(data.get("canary_tenants") or [])
        data["canary_users"] = frozenset(data.get("canary_users") or [])
        data["excluded_tenants"] = frozenset(data.get("excluded_tenants") or [])
        data["workload_cohorts"] = frozenset(data.get("workload_cohorts") or [])
        return TrafficPolicy(**data)

    def latest_policy_for_candidate(self, candidate_id: str) -> TrafficPolicy | None:
        with self._lock:
            rows = self._conn().execute(
                "SELECT payload_json FROM lc_policies WHERE candidate_id=? ORDER BY policy_id DESC LIMIT 1",
                (candidate_id,),
            ).fetchall()
        if not rows:
            return None
        data = json.loads(rows[0]["payload_json"])
        for key in ("internal_tenants", "test_tenants", "canary_tenants", "canary_users", "excluded_tenants", "workload_cohorts"):
            data[key] = frozenset(data.get(key) or [])
        return TrafficPolicy(**data)

    def save_rollout(self, state: RolloutState) -> RolloutState:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO lc_rollout(candidate_id, payload_json) VALUES (?, ?)",
                (state.candidate_id, _j(state.as_dict())),
            )
            self._conn().commit()
        return state

    def get_rollout(self, candidate_id: str) -> RolloutState | None:
        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM lc_rollout WHERE candidate_id=?", (candidate_id,)).fetchone()
        if not row:
            return None
        return RolloutState(**json.loads(row["payload_json"]))

    def save_evidence(self, evidence: LaunchEvidence) -> LaunchEvidence:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO lc_evidence(evidence_id, candidate_id, payload_json) VALUES (?, ?, ?)",
                (evidence.evidence_id, evidence.candidate_id, _j(evidence.as_dict())),
            )
            self._conn().commit()
        return evidence

    def list_evidence(self, candidate_id: str) -> list[LaunchEvidence]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT payload_json FROM lc_evidence WHERE candidate_id=? ORDER BY evidence_id",
                (candidate_id,),
            ).fetchall()
        return [LaunchEvidence(**json.loads(r["payload_json"])) for r in rows]

    def append_audit(self, event: dict) -> dict:
        with self._lock:
            self._conn().execute(
                "INSERT INTO lc_audit(candidate_id, payload_json) VALUES (?, ?)",
                (event.get("candidate_id"), _j(event)),
            )
            self._conn().commit()
        return event

    def list_audit(self, *, candidate_id: str | None = None) -> list[dict]:
        with self._lock:
            if candidate_id:
                rows = self._conn().execute(
                    "SELECT payload_json FROM lc_audit WHERE candidate_id=? ORDER BY event_id",
                    (candidate_id,),
                ).fetchall()
            else:
                rows = self._conn().execute("SELECT payload_json FROM lc_audit ORDER BY event_id").fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def list_all_evidence(self) -> list[LaunchEvidence]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT payload_json FROM lc_evidence ORDER BY evidence_id"
            ).fetchall()
        return [LaunchEvidence(**json.loads(r["payload_json"])) for r in rows]

    def save_launch_policy(self, policy) -> object:
        from controlled_launch.policy import ControlledLaunchPolicy

        if not isinstance(policy, ControlledLaunchPolicy):
            raise TypeError("ControlledLaunchPolicy required")
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO lc_launch_policies(policy_id, release_identity, payload_json) VALUES (?, ?, ?)",
                (policy.policy_id, policy.release_identity, _j(policy.as_dict())),
            )
            self._conn().commit()
        return policy

    def get_launch_policy(self, policy_id: str):
        from controlled_launch.policy import ControlledLaunchPolicy

        with self._lock:
            row = self._conn().execute(
                "SELECT payload_json FROM lc_launch_policies WHERE policy_id=?",
                (policy_id,),
            ).fetchone()
        if not row:
            return None
        return ControlledLaunchPolicy.from_dict(json.loads(row["payload_json"]))

    def latest_launch_policy(self, release_identity: str = ""):
        from controlled_launch.policy import ControlledLaunchPolicy

        with self._lock:
            if release_identity:
                rows = self._conn().execute(
                    "SELECT payload_json FROM lc_launch_policies WHERE release_identity=? ORDER BY policy_id DESC LIMIT 1",
                    (release_identity,),
                ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT payload_json FROM lc_launch_policies ORDER BY policy_id DESC LIMIT 1"
                ).fetchall()
        if not rows:
            return None
        return ControlledLaunchPolicy.from_dict(json.loads(rows[0]["payload_json"]))

    def set_launch_state(self, key: str, payload: dict) -> dict:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO lc_launch_state(key, payload_json) VALUES (?, ?)",
                (key, _j(payload)),
            )
            self._conn().commit()
        return payload

    def get_launch_state(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn().execute(
                "SELECT payload_json FROM lc_launch_state WHERE key=?",
                (key,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()
