"""Automatic controlled-launch containment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CONTAINMENT_CONTINUE = "CONTINUE"
CONTAINMENT_DEGRADE = "DEGRADE"
CONTAINMENT_PAUSE = "PAUSE_ADMISSION"
CONTAINMENT_KILL = "KILL_CONTROLLED_LAUNCH"


@dataclass(frozen=True)
class ContainmentDecision:
    action: str
    reason_code: str
    signals: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "reason_code": self.reason_code, "signals": dict(self.signals)}


class ContainmentEvaluator:
    """Deterministic containment from policy thresholds + observed signals."""

    def evaluate(self, *, signals: dict[str, float], thresholds: dict[str, Any] | None = None) -> ContainmentDecision:
        thresholds = thresholds or {}
        error_rate = float(signals.get("error_rate") or 0.0)
        security_failures = float(signals.get("security_failures") or 0.0)
        queue_saturation = float(signals.get("queue_saturation") or 0.0)
        provider_failures = float(signals.get("provider_failures") or 0.0)
        cost_ratio = float(signals.get("cost_ratio") or 0.0)
        critical_alert = float(signals.get("critical_alert") or 0.0)
        isolation_anomaly = float(signals.get("isolation_anomaly") or 0.0)

        if security_failures > 0 or isolation_anomaly > 0 or critical_alert > 0:
            return ContainmentDecision(CONTAINMENT_KILL, "zero_tolerance_security", signals)
        kill_cost = float(thresholds.get("kill_cost_ratio") or 1.0)
        if cost_ratio >= kill_cost and kill_cost > 0:
            return ContainmentDecision(CONTAINMENT_KILL, "budget_hard_ceiling", signals)
        pause_error = float(thresholds.get("pause_error_rate") or 0.25)
        if error_rate >= pause_error:
            return ContainmentDecision(CONTAINMENT_PAUSE, "elevated_error_rate", signals)
        pause_queue = float(thresholds.get("pause_queue_saturation") or 0.9)
        if queue_saturation >= pause_queue:
            return ContainmentDecision(CONTAINMENT_PAUSE, "queue_saturation", signals)
        degrade_provider = float(thresholds.get("degrade_provider_failures") or 5)
        if provider_failures >= degrade_provider:
            return ContainmentDecision(CONTAINMENT_DEGRADE, "provider_failures", signals)
        return ContainmentDecision(CONTAINMENT_CONTINUE, "within_thresholds", signals)
