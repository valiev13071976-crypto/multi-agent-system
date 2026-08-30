"""Production FinOps watch."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProductionFinOpsWatch:
    cost_per_request: float = 0.0
    total_cost: float = 0.0
    budget_limit: float = 0.0
    runaway_detected: bool = False

    def record(self, cost: float, *, requests: int = 1) -> None:
        self.total_cost += cost
        self.cost_per_request = self.total_cost / max(1, requests)
        if self.budget_limit > 0 and self.total_cost > self.budget_limit:
            self.runaway_detected = True

    def blocks_acceptance(self) -> bool:
        return self.runaway_detected

    def as_dict(self) -> dict:
        return {
            "cost_per_request": self.cost_per_request,
            "total_cost": self.total_cost,
            "budget_limit": self.budget_limit,
            "runaway_detected": self.runaway_detected,
        }
