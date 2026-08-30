"""Machine-readable capacity snapshot for autoscalers (Scale 3.24)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from task_queue.lanes import EXECUTION_LANES
from task_queue.models import (
    STATUS_DEAD_LETTERED,
    STATUS_LEASED,
    STATUS_QUEUED,
    STATUS_RETRY_WAIT,
    STATUS_RUNNING,
)
from task_queue.pools import POOL_BATCH, POOL_INTERACTIVE, POOL_NORMAL, pool_for_lane


PENDING = frozenset({STATUS_QUEUED, STATUS_RETRY_WAIT})
RUNNING = frozenset({STATUS_LEASED, STATUS_RUNNING})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CapacitySnapshot:
    queue_depth_by_lane: dict[str, int] = field(default_factory=dict)
    active_workers: int = 0
    saturated_pools: list[str] = field(default_factory=list)
    oldest_queued_age_seconds: float | None = None
    rejection_counts: dict[str, int] = field(default_factory=dict)
    utilization: dict[str, float] = field(default_factory=dict)
    dlq_depth: int = 0
    running_by_lane: dict[str, int] = field(default_factory=dict)
    checked_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue_depth_by_lane": dict(self.queue_depth_by_lane),
            "active_workers": int(self.active_workers),
            "saturated_pools": list(self.saturated_pools),
            "oldest_queued_age_seconds": self.oldest_queued_age_seconds,
            "rejection_counts": dict(self.rejection_counts),
            "utilization": dict(self.utilization),
            "dlq_depth": int(self.dlq_depth),
            "running_by_lane": dict(self.running_by_lane),
            "checked_at": self.checked_at,
        }


def build_capacity_snapshot(
    queue,
    *,
    admission_metrics: Mapping[str, int] | None = None,
    max_running_global: int | None = None,
    now: datetime | None = None,
) -> CapacitySnapshot:
    stamp = now or _utc_now()
    store = getattr(queue, "store", None)
    items = []
    if store is not None and hasattr(store, "list_all"):
        items = list(store.list_all())

    depth = {lane: 0 for lane in EXECUTION_LANES}
    running = {lane: 0 for lane in EXECUTION_LANES}
    oldest_age: float | None = None
    active = 0
    dlq = 0

    for item in items:
        lane = getattr(item, "execution_lane", None) or "background"
        status = getattr(item, "status", "")
        if status in PENDING:
            depth[lane] = depth.get(lane, 0) + 1
            created = getattr(item, "created_at", None)
            if created is not None:
                age = max(0.0, (stamp - created).total_seconds())
                oldest_age = age if oldest_age is None else max(oldest_age, age)
        elif status in RUNNING:
            # Expired leases count as pending for capacity, not active workers.
            if (
                status == STATUS_LEASED
                and getattr(item, "lease_expires_at", None) is not None
                and item.lease_expires_at <= stamp
            ):
                depth[lane] = depth.get(lane, 0) + 1
                continue
            running[lane] = running.get(lane, 0) + 1
            active += 1
        elif status == STATUS_DEAD_LETTERED:
            dlq += 1

    lim = getattr(queue, "admission_limits", None)
    cap = max_running_global
    if cap is None and lim is not None:
        cap = getattr(lim, "max_running_global", None)

    saturated: list[str] = []
    utilization: dict[str, float] = {}
    pool_running = {POOL_INTERACTIVE: 0, POOL_NORMAL: 0, POOL_BATCH: 0}
    for lane, count in running.items():
        pool = pool_for_lane(lane)
        pool_running[pool] = pool_running.get(pool, 0) + int(count)

    if cap is not None and int(cap) > 0:
        util = active / float(cap)
        utilization["global"] = round(util, 4)
        if util >= 1.0:
            saturated.extend([p for p, c in pool_running.items() if c > 0])
        # Pool saturation heuristic: interactive share of reserved capacity.
        reserved = int(getattr(lim, "interactive_reserved_running", 0) or 0) if lim else 0
        if reserved > 0 and pool_running.get(POOL_INTERACTIVE, 0) >= reserved:
            if POOL_INTERACTIVE not in saturated:
                saturated.append(POOL_INTERACTIVE)
        bg_cap = max(0, int(cap) - reserved) if reserved else int(cap)
        if bg_cap > 0:
            bg_active = pool_running.get(POOL_NORMAL, 0) + pool_running.get(POOL_BATCH, 0)
            utilization["background_pools"] = round(bg_active / float(bg_cap), 4)
            if bg_active >= bg_cap:
                for p in (POOL_NORMAL, POOL_BATCH):
                    if pool_running.get(p, 0) > 0 and p not in saturated:
                        saturated.append(p)
    else:
        utilization["global"] = 0.0

    rejections = dict(admission_metrics or {})
    try:
        from runtime.metrics import RUNTIME_COUNTERS

        for key in ("quota_reject", "overload_reject"):
            total = sum(RUNTIME_COUNTERS.by_lane(key).values())
            if total:
                rejections[key] = int(rejections.get(key, 0)) + int(total)
    except Exception:
        pass

    return CapacitySnapshot(
        queue_depth_by_lane=depth,
        active_workers=active,
        saturated_pools=saturated,
        oldest_queued_age_seconds=oldest_age,
        rejection_counts=rejections,
        utilization=utilization,
        dlq_depth=dlq,
        running_by_lane=running,
        checked_at=stamp.isoformat(),
    )
