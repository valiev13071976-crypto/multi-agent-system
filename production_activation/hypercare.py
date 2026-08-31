"""Bounded hypercare observation window — durable-session capable."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class HypercareWindow:
    candidate_id: str
    started_at: datetime
    policy: dict[str, Any]
    requests: int = 0
    p0_count: int = 0
    p1_count: int = 0
    ended_at: datetime | None = None
    plan_id: str = ""
    release_identity: str = ""
    status: str = "RUNNING"
    metrics_provided: bool = False

    def record_request(self) -> None:
        self.requests += 1
        self.metrics_provided = True

    def record_incident(self, *, severity: str) -> None:
        if severity == "P0":
            self.p0_count += 1
        elif severity == "P1":
            self.p1_count += 1
        self.metrics_provided = True

    def apply_metrics(self, *, requests: int | None = None, p0_count: int | None = None, p1_count: int | None = None) -> None:
        if requests is None and p0_count is None and p1_count is None:
            return
        if requests is not None:
            self.requests = int(requests)
        if p0_count is not None:
            self.p0_count = int(p0_count)
        if p1_count is not None:
            self.p1_count = int(p1_count)
        self.metrics_provided = True

    def within_window(self) -> bool:
        max_seconds = float(self.policy.get("max_window_seconds") or 3600)
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return elapsed <= max_seconds

    def can_exit(self) -> bool:
        min_requests = int(self.policy.get("min_requests") or 0)
        if self.p0_count > 0 or self.p1_count > 0:
            return False
        if self.requests < min_requests:
            return False
        return True

    def complete(self, *, require_metrics: bool = False) -> dict[str, Any]:
        self.ended_at = datetime.now(timezone.utc)
        if require_metrics and not self.metrics_provided:
            self.status = "FAIL"
            return {
                "candidate_id": self.candidate_id,
                "plan_id": self.plan_id,
                "release_identity": self.release_identity,
                "status": "FAIL",
                "reason": "missing_hypercare_metrics",
                "requests": self.requests,
                "p0_count": self.p0_count,
                "p1_count": self.p1_count,
                "duration_seconds": (self.ended_at - self.started_at).total_seconds(),
                "started_at": self.started_at.isoformat(),
                "completed_at": self.ended_at.isoformat(),
                "evidence_kind": "hypercare",
            }
        status = "PASS" if self.can_exit() and self.p0_count == 0 and self.p1_count == 0 else "FAIL"
        self.status = status
        return {
            "candidate_id": self.candidate_id,
            "plan_id": self.plan_id,
            "release_identity": self.release_identity,
            "status": status,
            "requests": self.requests,
            "p0_count": self.p0_count,
            "p1_count": self.p1_count,
            "duration_seconds": (self.ended_at - self.started_at).total_seconds(),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.ended_at.isoformat(),
            "evidence_kind": "hypercare",
        }

    def as_session(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "plan_id": self.plan_id,
            "release_identity": self.release_identity,
            "started_at": self.started_at.isoformat(),
            "status": self.status,
            "policy": dict(self.policy),
            "requests": self.requests,
            "p0_count": self.p0_count,
            "p1_count": self.p1_count,
            "metrics_provided": self.metrics_provided,
            "ended_at": self.ended_at.isoformat() if self.ended_at else "",
            "evidence_kind": "hypercare",
        }

    @classmethod
    def from_session(cls, session: dict[str, Any]) -> HypercareWindow:
        started = datetime.fromisoformat(str(session["started_at"]))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        ended = None
        if session.get("ended_at"):
            ended = datetime.fromisoformat(str(session["ended_at"]))
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
        return cls(
            candidate_id=str(session.get("candidate_id") or ""),
            plan_id=str(session.get("plan_id") or ""),
            release_identity=str(session.get("release_identity") or ""),
            started_at=started,
            policy=dict(session.get("policy") or {}),
            requests=int(session.get("requests") or 0),
            p0_count=int(session.get("p0_count") or 0),
            p1_count=int(session.get("p1_count") or 0),
            ended_at=ended,
            status=str(session.get("status") or "RUNNING"),
            metrics_provided=bool(session.get("metrics_provided")),
        )
