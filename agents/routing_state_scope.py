"""Routing health / runtime-stats state-scope contract (PATCH-MR-05).

CURRENT: ProviderHealthTracker and ProviderRuntimeStatsAggregator are
process-local in-memory stores. Independent worker processes do not share
cooldown or runtime-stat samples.

SAFE: single-process / combined deployments, or multi-worker deployments that
explicitly accept per-worker health/stat isolation.

NOT YET GUARANTEED: cross-worker cooldown or runtime-stat synchronization.

FUTURE: a shared backing store may implement the protocols below during Scale/HA
work. This module does not implement Redis/DB/pub-sub.

Operational signals:
- routing_health_scope = process_local
- routing_runtime_stats_scope = process_local
- routing_health_shared_backing = not_available
- multi_worker_shared_routing_health_ready = false

Liveness remains independent of shared-health availability. Single-process
deployments stay valid without shared backing.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

STATE_SCOPE_PROCESS_LOCAL = "process_local"
STATE_SCOPE_SHARED = "shared"

SHARED_BACKING_AVAILABLE = "available"
SHARED_BACKING_NOT_AVAILABLE = "not_available"


@runtime_checkable
class ProviderHealthStore(Protocol):
    """Replaceable health store seam. Default impl is process-local memory."""

    @property
    def state_scope(self) -> str:
        ...

    @property
    def shared_backing(self) -> bool:
        ...

    def record_failure(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        error_class: str = "",
        now=None,
    ):
        ...

    def record_success(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now=None,
    ):
        ...

    def snapshot(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now=None,
    ):
        ...

    def is_auto_eligible(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now=None,
    ) -> bool:
        ...


@runtime_checkable
class ProviderRuntimeStatsStore(Protocol):
    """Replaceable runtime-stats store seam. Default impl is process-local."""

    @property
    def state_scope(self) -> str:
        ...

    @property
    def shared_backing(self) -> bool:
        ...

    def record_success(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        latency_ms: float | None = None,
        cost=None,
        now=None,
    ):
        ...

    def record_failure(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        latency_ms: float | None = None,
        now=None,
    ):
        ...

    def snapshot(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now=None,
    ):
        ...


def routing_coordination_capabilities(
    *,
    health_tracker: Any | None = None,
    runtime_stats: Any | None = None,
) -> dict[str, Any]:
    """Machine-readable coordination capability report (no secrets/prompts)."""

    health_scope = STATE_SCOPE_PROCESS_LOCAL
    health_shared = False
    if health_tracker is not None:
        health_scope = str(
            getattr(health_tracker, "state_scope", STATE_SCOPE_PROCESS_LOCAL)
            or STATE_SCOPE_PROCESS_LOCAL
        )
        # Fail-safe: only claim shared when store reports shared_backing True
        # AND available is not explicitly False.
        backing = bool(getattr(health_tracker, "shared_backing", False))
        available = getattr(health_tracker, "available", True)
        health_shared = bool(backing and available is not False)

    stats_scope = STATE_SCOPE_PROCESS_LOCAL
    stats_shared = False
    if runtime_stats is not None:
        stats_scope = str(
            getattr(runtime_stats, "state_scope", STATE_SCOPE_PROCESS_LOCAL)
            or STATE_SCOPE_PROCESS_LOCAL
        )
        stats_shared = bool(getattr(runtime_stats, "shared_backing", False))

    shared_ready = (
        health_shared
        and health_scope == STATE_SCOPE_SHARED
        and getattr(health_tracker, "available", True) is not False
    )
    return {
        "routing_health_scope": health_scope,
        "routing_runtime_stats_scope": stats_scope,
        "routing_health_shared_backing": (
            SHARED_BACKING_AVAILABLE if health_shared else SHARED_BACKING_NOT_AVAILABLE
        ),
        "routing_runtime_stats_shared_backing": (
            SHARED_BACKING_AVAILABLE if stats_shared else SHARED_BACKING_NOT_AVAILABLE
        ),
        "multi_worker_shared_routing_health_ready": bool(shared_ready),
        "runtime_stats_tiebreak_enabled_default": False,
    }
