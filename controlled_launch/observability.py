"""Stage-4 observability metrics."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LaunchMetrics:
    launch_requests_total: int = 0
    launch_errors_total: int = 0
    shadow_requests_total: int = 0
    shadow_failures_total: int = 0
    canary_requests_total: int = 0
    canary_errors_total: int = 0
    rollout_hold_total: int = 0
    rollout_abort_total: int = 0
    rollout_rollback_total: int = 0
    guardrail_violation_total: int = 0
    side_effect_denial_total: int = 0
    candidate_cost: float = 0.0
    candidate_latency_ms: float = 0.0
    _labels: dict[str, str] = field(default_factory=dict)

    def inc(self, name: str, *, value: int = 1) -> None:
        current = getattr(self, name, 0)
        setattr(self, name, current + value)

    def observe_latency(self, ms: float) -> None:
        self.candidate_latency_ms = ms

    def observe_cost(self, cost: float) -> None:
        self.candidate_cost += cost

    def as_dict(self) -> dict:
        return {
            "launch_requests_total": self.launch_requests_total,
            "launch_errors_total": self.launch_errors_total,
            "shadow_requests_total": self.shadow_requests_total,
            "shadow_failures_total": self.shadow_failures_total,
            "canary_requests_total": self.canary_requests_total,
            "canary_errors_total": self.canary_errors_total,
            "rollout_hold_total": self.rollout_hold_total,
            "rollout_abort_total": self.rollout_abort_total,
            "rollout_rollback_total": self.rollout_rollback_total,
            "guardrail_violation_total": self.guardrail_violation_total,
            "side_effect_denial_total": self.side_effect_denial_total,
            "candidate_cost": self.candidate_cost,
            "candidate_latency_ms": self.candidate_latency_ms,
        }
