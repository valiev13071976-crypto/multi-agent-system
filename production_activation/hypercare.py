"""Bounded hypercare observation window."""

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

    def record_request(self) -> None:
        self.requests += 1

    def record_incident(self, *, severity: str) -> None:
        if severity == "P0":
            self.p0_count += 1
        elif severity == "P1":
            self.p1_count += 1

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

    def complete(self) -> dict[str, Any]:
        self.ended_at = datetime.now(timezone.utc)
        status = "PASS" if self.can_exit() and self.p0_count == 0 and self.p1_count == 0 else "FAIL"
        return {
            "candidate_id": self.candidate_id,
            "status": status,
            "requests": self.requests,
            "p0_count": self.p0_count,
            "p1_count": self.p1_count,
            "duration_seconds": (self.ended_at - self.started_at).total_seconds(),
        }
