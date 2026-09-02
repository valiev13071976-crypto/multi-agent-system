"""Autoscaling signal generation with cooldown and hysteresis (no infrastructure mutation)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from scale_optimization.config import (
    MIN_OBSERVATION_WINDOW_SECONDS,
    MIN_SAMPLE_COUNT,
    SCALE_COOLDOWN_SECONDS,
    SCALE_HYSTERESIS_RATIO,
)
from scale_optimization.models import (
    REC_DECREASE_POOL,
    REC_INCREASE_POOL,
    REC_INSUFFICIENT_DATA,
    REC_NO_ACTION,
    REC_SCALE_OUT,
    REC_SHED_LOAD,
    ScaleRecommendation,
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class AutoscalingSignalEngine:
    """Produces scale recommendations from metrics; never mutates infrastructure."""

    def __init__(
        self,
        *,
        cooldown_seconds: float = SCALE_COOLDOWN_SECONDS,
        hysteresis_ratio: float = SCALE_HYSTERESIS_RATIO,
        min_window_seconds: float = MIN_OBSERVATION_WINDOW_SECONDS,
        min_samples: int = MIN_SAMPLE_COUNT,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.hysteresis_ratio = hysteresis_ratio
        self.min_window_seconds = min_window_seconds
        self.min_samples = min_samples
        self._last_action: str | None = None
        self._last_action_at: str | None = None
        self._last_signal_level: float = 0.0

    def evaluate(
        self,
        *,
        queue_depth: float,
        oldest_age_seconds: float,
        arrival_completion_delta: float,
        worker_utilization: float,
        interactive_latency_p95_ms: float,
        provider_saturation: float,
        sample_count: int,
        observation_window_seconds: float,
        now_iso: str | None = None,
        workload_class: str = "INTERACTIVE",
    ) -> ScaleRecommendation:
        now = now_iso or datetime.now(timezone.utc).isoformat()
        if sample_count < self.min_samples or observation_window_seconds < self.min_window_seconds:
            return ScaleRecommendation(
                action=REC_INSUFFICIENT_DATA,
                reason="insufficient_observation",
                evidence={"sample_count": sample_count, "window": observation_window_seconds},
                workload_class=workload_class,
            )

        # Composite pressure signal in [0, ~4]
        signal = 0.0
        if queue_depth > 50:
            signal += 1.0
        if oldest_age_seconds > 60:
            signal += 1.0
        if arrival_completion_delta > 0:
            signal += 1.0
        if worker_utilization >= 0.85:
            signal += 1.0
        if interactive_latency_p95_ms > 2000:
            signal += 1.0
        if provider_saturation >= 0.9:
            signal += 0.5

        action = REC_NO_ACTION
        if signal >= 3.0 or (queue_depth > 200 and worker_utilization >= 0.9):
            action = REC_SCALE_OUT if worker_utilization >= 0.9 else REC_INCREASE_POOL
        elif signal >= 2.0:
            action = REC_INCREASE_POOL
        elif queue_depth > 500:
            action = REC_SHED_LOAD
        elif signal <= 0.5 and worker_utilization < 0.3 and queue_depth < 5:
            action = REC_DECREASE_POOL

        cooldown_ok = self._cooldown_ok(now)
        hysteresis_ok = self._hysteresis_ok(signal, action)

        if action != REC_NO_ACTION and action != REC_INSUFFICIENT_DATA:
            if not cooldown_ok:
                return ScaleRecommendation(
                    action=REC_NO_ACTION,
                    reason="cooldown_active",
                    evidence={"signal": signal, "last_action": self._last_action},
                    workload_class=workload_class,
                    cooldown_ok=False,
                    hysteresis_ok=hysteresis_ok,
                )
            if not hysteresis_ok:
                return ScaleRecommendation(
                    action=REC_NO_ACTION,
                    reason="hysteresis_hold",
                    evidence={"signal": signal, "last_level": self._last_signal_level},
                    workload_class=workload_class,
                    cooldown_ok=True,
                    hysteresis_ok=False,
                )

        if action not in {REC_NO_ACTION, REC_INSUFFICIENT_DATA}:
            self._last_action = action
            self._last_action_at = now
            self._last_signal_level = signal

        return ScaleRecommendation(
            action=action,
            reason="metric_driven_signal",
            evidence={
                "signal": signal,
                "queue_depth": queue_depth,
                "oldest_age_seconds": oldest_age_seconds,
                "arrival_completion_delta": arrival_completion_delta,
                "worker_utilization": worker_utilization,
                "interactive_latency_p95_ms": interactive_latency_p95_ms,
                "provider_saturation": provider_saturation,
            },
            workload_class=workload_class,
            cooldown_ok=True,
            hysteresis_ok=True,
        )

    def _cooldown_ok(self, now_iso: str) -> bool:
        last = _parse_ts(self._last_action_at)
        now = _parse_ts(now_iso)
        if last is None or now is None:
            return True
        return (now - last).total_seconds() >= self.cooldown_seconds

    def _hysteresis_ok(self, signal: float, action: str) -> bool:
        if self._last_action is None:
            return True
        # Prevent flapping: require signal move beyond hysteresis band vs last
        delta = abs(signal - self._last_signal_level)
        if action == self._last_action:
            return True
        # Opposite direction (up vs down) needs larger move
        up = action in {REC_SCALE_OUT, REC_INCREASE_POOL, REC_SHED_LOAD}
        last_up = self._last_action in {REC_SCALE_OUT, REC_INCREASE_POOL, REC_SHED_LOAD}
        if up != last_up:
            return delta >= max(1.0, self.hysteresis_ratio * 10)
        return delta >= self.hysteresis_ratio
