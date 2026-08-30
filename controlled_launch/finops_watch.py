"""Rollout FinOps watch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FinOpsWatch:
    candidate_cost: float = 0.0
    control_cost: float = 0.0
    cost_per_request: float = 0.0
    budget_limit: float = 0.0
    runaway_detected: bool = False

    def record(self, *, candidate: bool, cost: float, requests: int = 1) -> None:
        if candidate:
            self.candidate_cost += cost
        else:
            self.control_cost += cost
        total_requests = max(1, requests)
        self.cost_per_request = (self.candidate_cost + self.control_cost) / total_requests
        if self.budget_limit > 0 and self.candidate_cost > self.budget_limit:
            self.runaway_detected = True

    def delta(self) -> float | None:
        if self.control_cost <= 0:
            return None
        return self.candidate_cost - self.control_cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_cost": self.candidate_cost,
            "control_cost": self.control_cost,
            "cost_per_request": self.cost_per_request,
            "budget_limit": self.budget_limit,
            "runaway_detected": self.runaway_detected,
            "delta": self.delta(),
        }

    def recommended_action(self, *, regression_threshold: float = 0.5) -> str:
        if self.runaway_detected:
            return "ABORT"
        delta = self.delta()
        if delta is not None and self.control_cost > 0 and delta / self.control_cost > regression_threshold:
            return "HOLD"
        return "none"
