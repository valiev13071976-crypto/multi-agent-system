"""Alert condition evaluation from capacity snapshots (Scale 3.28+)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from runtime.capacity_snapshot import CapacitySnapshot


SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRIT = "crit"


@dataclass(frozen=True)
class AlertThresholds:
    queue_depth_high: int = 500
    oldest_job_age_seconds: float = 300.0
    overload_reject_high: int = 10
    dlq_depth_high: int = 20
    utilization_high: float = 0.95


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

    return alerts
