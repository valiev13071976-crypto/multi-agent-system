"""Alert condition evaluation from capacity snapshots (Scale 3.28+).

Scale 3.35 additions (capacity/saturation alerts): additive-only extensions
that preserve every existing call signature/behavior:

- ``evaluate_alert_conditions`` gained optional trusted-signal keyword args
  (``load_shed_active``, ``provider_governor_saturated``,
  ``stale_instance_count``) to cover the remaining required 3.35 alert
  families (persistent load shedding, provider-governor saturation,
  stale/dead runtime instances) using the SAME ``AlertCondition``/
  ``AlertThresholds`` contract -- no parallel alert type was introduced.
- ``AlertDebouncer`` is a new, separate, stateful wrapper that turns the
  existing stateless per-call ``evaluate_alert_conditions`` output into a
  non-flapping, threshold+duration+hysteresis alert state machine, without
  changing ``evaluate_alert_conditions`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from runtime.capacity_snapshot import CapacitySnapshot


SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRIT = "crit"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AlertThresholds:
    queue_depth_high: int = 500
    oldest_job_age_seconds: float = 300.0
    overload_reject_high: int = 10
    dlq_depth_high: int = 20
    utilization_high: float = 0.95
    # Scale 3.35: stale/dead runtime instance fleet signal.
    stale_instance_high: int = 1


@dataclass(frozen=True)
class AlertCondition:
    code: str
    severity: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "details": dict(self.details),
        }


def evaluate_alert_conditions(
    snapshot: CapacitySnapshot,
    thresholds: AlertThresholds | Mapping[str, Any] | None = None,
    *,
    worker_healthy: bool = True,
    governor_available: bool = True,
    # Scale 3.35: additional trusted runtime signals (all optional, all
    # additive -- omitting them preserves the exact pre-3.35 behavior).
    load_shed_active: bool = False,
    provider_governor_saturated: bool = False,
    stale_instance_count: int = 0,
) -> list[AlertCondition]:
    if isinstance(thresholds, Mapping):
        thr = AlertThresholds(
            queue_depth_high=int(thresholds.get("queue_depth_high", 500)),
            oldest_job_age_seconds=float(
                thresholds.get("oldest_job_age_seconds", 300.0)
            ),
            overload_reject_high=int(thresholds.get("overload_reject_high", 10)),
            dlq_depth_high=int(thresholds.get("dlq_depth_high", 20)),
            utilization_high=float(thresholds.get("utilization_high", 0.95)),
            stale_instance_high=int(thresholds.get("stale_instance_high", 1)),
        )
    else:
        thr = thresholds or AlertThresholds()

    alerts: list[AlertCondition] = []
    depth = dict(snapshot.queue_depth_by_lane or {})
    total_depth = sum(int(v) for v in depth.values())
    if total_depth >= thr.queue_depth_high:
        alerts.append(
            AlertCondition(
                "queue_depth_high",
                SEVERITY_WARN if total_depth < thr.queue_depth_high * 2 else SEVERITY_CRIT,
                {"total_depth": total_depth, "by_lane": depth},
            )
        )

    age = snapshot.oldest_queued_age_seconds
    if age is not None and age >= thr.oldest_job_age_seconds:
        alerts.append(
            AlertCondition(
                "oldest_job_age_high",
                SEVERITY_WARN,
                {"oldest_queued_age_seconds": age},
            )
        )

    if snapshot.saturated_pools:
        alerts.append(
            AlertCondition(
                "pool_saturated",
                SEVERITY_WARN,
                {"pools": list(snapshot.saturated_pools)},
            )
        )

    rejects = dict(snapshot.rejection_counts or {})
    overload = int(rejects.get("overload_reject", 0) or rejects.get("global_pending_limit", 0))
    if overload >= thr.overload_reject_high:
        alerts.append(
            AlertCondition(
                "overload_repeated",
                SEVERITY_CRIT,
                {"overload_reject": overload},
            )
        )

    if int(snapshot.dlq_depth or 0) >= thr.dlq_depth_high:
        alerts.append(
            AlertCondition(
                "dlq_growth",
                SEVERITY_WARN,
                {"dlq_depth": int(snapshot.dlq_depth)},
            )
        )

    util = float((snapshot.utilization or {}).get("global", 0.0) or 0.0)
    if util >= thr.utilization_high and "pool_saturated" not in {a.code for a in alerts}:
        # utilization alone surfaces as pool_saturated when pools listed; else info.
        pass

    if not worker_healthy:
        alerts.append(
            AlertCondition("worker_unhealthy", SEVERITY_CRIT, {"healthy": False})
        )

    if not governor_available:
        alerts.append(
            AlertCondition(
                "governor_unavailable",
                SEVERITY_CRIT,
                {"available": False},
            )
        )

    # Scale 3.35: persistent system-level load shedding (runtime.load_shedding,
    # Scale 3.32) is itself an alertable saturation signal, distinct from a
    # single transient shed decision.
    if load_shed_active:
        alerts.append(
            AlertCondition("persistent_load_shed", SEVERITY_CRIT, {"load_shed_active": True})
        )

    # Scale 3.35: distributed ProviderGovernor (Scale 3.30) circuit/breaker
    # saturation, shared across instances.
    if provider_governor_saturated:
        alerts.append(
            AlertCondition(
                "provider_governor_saturated",
                SEVERITY_CRIT,
                {"provider_governor_saturated": True},
            )
        )

    # Scale 3.35: fleet registry (Scale 3.29) stale/dead instance detection.
    if int(stale_instance_count or 0) >= thr.stale_instance_high:
        alerts.append(
            AlertCondition(
                "stale_runtime_instances",
                SEVERITY_WARN,
                {"stale_instance_count": int(stale_instance_count)},
            )
        )

    return alerts


@dataclass
class AlertRuleState:
    """Mutable per-code debounce/hysteresis state (process-local, observable)."""

    firing: bool = False
    breach_since: datetime | None = None
    last_seen_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "firing": self.firing,
            "breach_since": self.breach_since.isoformat() if self.breach_since else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


@dataclass
class AlertDebouncer:
    """Threshold + duration + hysteresis wrapper (Scale 3.35).

    ``evaluate_alert_conditions`` itself remains a pure, stateless,
    single-snapshot evaluator (preserved exactly for backward
    compatibility). This wrapper adds an observable, testable anti-flapping
    layer on top: a raw alert code must be continuously PRESENT for
    ``fire_after_seconds`` before the debounced state fires, and
    continuously ABSENT for ``clear_after_seconds`` before it clears -- a
    condition oscillating near a threshold does not flap the alert.
    """

    fire_after_seconds: float = 30.0
    clear_after_seconds: float = 30.0
    _states: dict[str, AlertRuleState] = field(default_factory=dict, repr=False)

    def observe(self, active_codes, *, now: datetime | None = None) -> dict[str, bool]:
        """Feed the current raw ``{condition.code for condition in alerts}``
        set; returns ``{code: debounced_firing_bool}`` for every code ever
        observed (bounded by the fixed, small set of known alert codes)."""

        stamp = now or _utc_now()
        codes = set(active_codes)
        all_codes = codes | set(self._states.keys())
        result: dict[str, bool] = {}
        for code in all_codes:
            st = self._states.setdefault(code, AlertRuleState())
            present = code in codes
            if present:
                if st.breach_since is None:
                    st.breach_since = stamp
                st.last_seen_at = stamp
                if not st.firing:
                    elapsed = (stamp - st.breach_since).total_seconds()
                    if elapsed >= self.fire_after_seconds:
                        st.firing = True
            else:
                if st.firing:
                    cleared_elapsed = (
                        (stamp - st.last_seen_at).total_seconds()
                        if st.last_seen_at is not None
                        else self.clear_after_seconds
                    )
                    if cleared_elapsed >= self.clear_after_seconds:
                        st.firing = False
                        st.breach_since = None
                else:
                    st.breach_since = None
            result[code] = st.firing
        return result

    def snapshot(self) -> dict[str, dict]:
        return {code: st.as_dict() for code, st in self._states.items()}

    def any_firing(self) -> bool:
        return any(st.firing for st in self._states.values())
