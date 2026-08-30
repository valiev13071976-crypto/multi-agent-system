"""Canary traffic assignment + immediate disable (Scale 3.37).

Uses RoutingActivationService for rollback path. Does not mutate offline
PromotionGovernor state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from evals.activation import RoutingActivationService
from evals.models import utc_now


@dataclass
class CanaryMetrics:
    assigned: int = 0
    skipped: int = 0
    enabled_at: datetime | None = None
    disabled_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "assigned": self.assigned,
            "skipped": self.skipped,
            "enabled_at": self.enabled_at.isoformat() if self.enabled_at else None,
            "disabled_at": self.disabled_at.isoformat() if self.disabled_at else None,
        }


@dataclass
class CanaryController:
    """Deterministic percent canary with immediate disable + activation rollback."""

    activation: RoutingActivationService | None = None
    candidate_id: str | None = None
    percent: int = 0
    policy_version: str = ""
    enabled: bool = False
    metrics: CanaryMetrics = field(default_factory=CanaryMetrics)

    def __post_init__(self):
        if self.activation is None:
            self.activation = RoutingActivationService()
        self.percent = self._bound_percent(self.percent)

    @staticmethod
    def _bound_percent(value: int | float) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, min(100, n))

    def enable(
        self,
        candidate_id: str,
        percent: int,
        policy_version: str,
        *,
        now: datetime | None = None,
    ) -> None:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        self.candidate_id = str(candidate_id or "").strip()
        self.percent = self._bound_percent(percent)
        self.policy_version = str(policy_version or "")
        self.enabled = bool(self.candidate_id) and self.percent > 0
        self.metrics.enabled_at = stamp
        self.metrics.disabled_at = None

    def disable(self, *, now: datetime | None = None) -> None:
        """Immediate disable — no new canary assignments."""

        stamp = now or utc_now()
        self.enabled = False
        self.percent = 0
        self.metrics.disabled_at = stamp

    def assign(self, request_id: str) -> bool:
        """Deterministic hash assignment. False when disabled or percent=0."""

        if not self.enabled or self.percent <= 0 or not self.candidate_id:
            self.metrics.skipped += 1
            return False
        digest = hashlib.sha256(
            f"{self.candidate_id}:{self.policy_version}:{request_id}".encode("utf-8")
        ).hexdigest()
        bucket = int(digest[:8], 16) % 100
        hit = bucket < self.percent
        if hit:
            self.metrics.assigned += 1
        else:
            self.metrics.skipped += 1
        return hit

    def rollback(self, actor_ref: str, *, now: datetime | None = None):
        """Disable canary and roll back via RoutingActivationService."""

        self.disable(now=now)
        assert self.activation is not None
        return self.activation.rollback(actor_ref, now=now)
