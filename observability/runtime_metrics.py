"""Aggregated operational + interactive SLO signals (Phase 3 Block 3).

Does not expose tenant IDs publicly — tenant counts are hashed/bucketed when included.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from task_queue.lanes import LANE_INTERACTIVE, EXECUTION_LANES
from task_queue.models import STATUS_LEASED, STATUS_QUEUED, STATUS_RETRY_WAIT, STATUS_RUNNING


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _tenant_bucket(tenant_id: str) -> str:
    """Low-cardinality anonymized tenant label for ops (not reversible identity)."""

    digest = hashlib.sha256(str(tenant_id or "").encode("utf-8")).hexdigest()[:8]
    return f"t:{digest}"


@dataclass
class LatencyAgg:
    count: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0

    def observe(self, ms: float) -> None:
        self.count += 1
        self.sum_ms += float(ms)
        self.max_ms = max(self.max_ms, float(ms))

    def as_dict(self) -> dict:
        avg = (self.sum_ms / self.count) if self.count else 0.0
        return {
            "count": self.count,
            "avg_ms": round(avg, 3),
            "max_ms": round(self.max_ms, 3),
            "sum_ms": round(self.sum_ms, 3),
        }


@dataclass
class RuntimeMetricsRegistry:
    """Process-local counters for admission/SLO; queue depths read from durable store."""

    admission_accept: int = 0
    admission_reject: int = 0
    admission_defer: int = 0
    admission_accept_interactive: int = 0
    admission_reject_interactive: int = 0
    admission_defer_interactive: int = 0
    worker_claims: int = 0
    lease_expirations: int = 0
    reclaims: int = 0
    provider_throttle: int = 0
    provider_429: int = 0
    budget_rejections: int = 0
    claim_latency: LatencyAgg = field(default_factory=LatencyAgg)
    workflow_persist_latency: LatencyAgg = field(default_factory=LatencyAgg)
    budget_reserve_latency: LatencyAgg = field(default_factory=LatencyAgg)
    governor_acquire_latency: LatencyAgg = field(default_factory=LatencyAgg)
    interactive_queue_wait: LatencyAgg = field(default_factory=LatencyAgg)
    interactive_execution_latency: LatencyAgg = field(default_factory=LatencyAgg)

    def record_admission(self, decision: str, *, lane: str = "") -> None:
        d = str(decision or "").upper()
        interactive = lane == LANE_INTERACTIVE
        if d == "ACCEPT":
            self.admission_accept += 1
            if interactive:
                self.admission_accept_interactive += 1
        elif d == "DEFER":
            self.admission_defer += 1
            if interactive:
                self.admission_defer_interactive += 1
        else:
            self.admission_reject += 1
            if interactive:
                self.admission_reject_interactive += 1


# Process singleton
RUNTIME_METRICS = RuntimeMetricsRegistry()


def collect_queue_snapshot(queue_or_store) -> dict[str, Any]:
    store = getattr(queue_or_store, "store", queue_or_store)
    now = _utc_now()
    depth_by_lane = {lane: 0 for lane in EXECUTION_LANES}
    oldest_age_by_lane: dict[str, float | None] = {lane: None for lane in EXECUTION_LANES}
    pending_global = running_global = 0
    pending_by_tenant: dict[str, int] = {}
    running_by_tenant: dict[str, int] = {}
    interactive_oldest = None

    items = []
    if hasattr(store, "list_all"):
        try:
            items = list(store.list_all())
        except Exception:
            items = []
    elif hasattr(store, "count_by_status"):
        try:
            counts = store.count_by_status()
            return {
                "queue_depth_by_lane": dict(counts.get("pending_by_lane") or {}),
                "running_by_lane": dict(counts.get("running_by_lane") or {}),
                "pending_global": counts.get("pending_global", 0),
                "running_global": counts.get("running_global", 0),
                "pending_by_tenant": {},
                "running_by_tenant": {},
                "oldest_queue_age_by_lane": {},
                "interactive_oldest_queue_age": None,
                "sqlite_busy_count": getattr(store, "sqlite_busy_count", 0),
                "capacity_throttle_count": getattr(store, "capacity_throttle_count", 0),
            }
        except Exception:
            pass

    for item in items:
        lane = getattr(item, "execution_lane", None) or "background"
        status = getattr(item, "status", "")
        tenant = _tenant_bucket(getattr(item, "tenant_id", "") or "")
        created = getattr(item, "created_at", None)
        age = None
        if isinstance(created, datetime):
            age = max(0.0, (now - created).total_seconds())
        if status in {STATUS_QUEUED, STATUS_RETRY_WAIT}:
            pending_global += 1
            depth_by_lane[lane] = depth_by_lane.get(lane, 0) + 1
            pending_by_tenant[tenant] = pending_by_tenant.get(tenant, 0) + 1
            if age is not None:
                prev = oldest_age_by_lane.get(lane)
                oldest_age_by_lane[lane] = age if prev is None else max(prev, age)
                if lane == LANE_INTERACTIVE:
                    interactive_oldest = (
                        age if interactive_oldest is None else max(interactive_oldest, age)
                    )
        elif status in {STATUS_LEASED, STATUS_RUNNING}:
            lease_exp = getattr(item, "lease_expires_at", None)
            if (
                status == STATUS_LEASED
                and lease_exp is not None
                and isinstance(lease_exp, datetime)
                and lease_exp <= now
            ):
                continue
            running_global += 1
            running_by_tenant[tenant] = running_by_tenant.get(tenant, 0) + 1

    return {
        "queue_depth_by_lane": depth_by_lane,
        "oldest_queue_age_by_lane": oldest_age_by_lane,
        "pending_global": pending_global,
        "running_global": running_global,
        "pending_by_tenant": pending_by_tenant,
        "running_by_tenant": running_by_tenant,
        "interactive_oldest_queue_age": interactive_oldest,
        "sqlite_busy_count": getattr(store, "sqlite_busy_count", 0),
        "capacity_throttle_count": getattr(store, "capacity_throttle_count", 0),
    }


def collect_operational_metrics(
    *,
    side_effect_runtime=None,
    provider_governor=None,
    metrics_registry: RuntimeMetricsRegistry | None = None,
    health_tracker=None,
    runtime_stats=None,
) -> dict[str, Any]:
    from agents.routing_state_scope import routing_coordination_capabilities

    reg = metrics_registry or RUNTIME_METRICS
    wr = getattr(side_effect_runtime, "workflow_runtime", None) if side_effect_runtime else None
    queue = getattr(wr, "queue", None) if wr is not None else None
    qsnap = collect_queue_snapshot(queue) if queue is not None else {}

    breaker_states = {}
    provider_active = 0
    if provider_governor is not None:
        store = getattr(provider_governor, "store", None)
        # Best-effort: in-memory slots count
        slots = getattr(store, "_slots", None)
        if isinstance(slots, dict):
            provider_active = len(slots)
        for provider in ("openai", "anthropic", "gemini", "grok"):
            try:
                breaker_states[provider] = provider_governor.breaker_state(provider, "")
            except Exception:
                continue

    interactive_total = (
        reg.admission_accept_interactive
        + reg.admission_reject_interactive
        + reg.admission_defer_interactive
    ) or 1

    routing_caps = routing_coordination_capabilities(
        health_tracker=health_tracker,
        runtime_stats=runtime_stats,
    )

    return {
        "collected_at": time.time(),
        **qsnap,
        "worker_claims": reg.worker_claims,
        "lease_expirations": reg.lease_expirations,
        "reclaims": reg.reclaims,
        "admission_accept": reg.admission_accept,
        "admission_reject": reg.admission_reject,
        "admission_defer": reg.admission_defer,
        "provider_active": provider_active,
        "provider_throttle": reg.provider_throttle,
        "provider_429": reg.provider_429,
        "provider_breaker_state": breaker_states,
        "budget_rejections": reg.budget_rejections,
        "claim_latency": reg.claim_latency.as_dict(),
        "workflow_persist_latency": reg.workflow_persist_latency.as_dict(),
        "budget_reservation_latency": reg.budget_reserve_latency.as_dict(),
        "governor_acquire_latency": reg.governor_acquire_latency.as_dict(),
        "routing_coordination": routing_caps,
        "interactive_slo": {
            "interactive_queue_wait": reg.interactive_queue_wait.as_dict(),
            "interactive_execution_latency": reg.interactive_execution_latency.as_dict(),
            "interactive_oldest_queue_age": qsnap.get("interactive_oldest_queue_age"),
            "interactive_admission_reject_rate": round(
                reg.admission_reject_interactive / interactive_total, 4
            ),
            "interactive_admission_defer_rate": round(
                reg.admission_defer_interactive / interactive_total, 4
            ),
        },
    }
