"""Liveness / readiness / drain contracts (Phase 3 Block 3).

Liveness = process is alive (cheap).
Readiness = role can accept/perform its work (bounded dependency checks).

PATCH-MR-05: routing provider health and runtime stats are process-local.
``HealthSnapshot.capabilities`` and informational dependency details expose
scope=process_local and shared_backing=not_available. Missing shared
cross-worker routing health does **not** fail liveness or ordinary readiness
for single-process / deployments that accept per-worker isolation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from config.runtime_config import RuntimeConfig, validate_runtime_config
from workflow.runtime_role import ROLE_API, ROLE_COMBINED, ROLE_WORKER, role_runs_worker_loops

STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_NOT_READY = "not_ready"


@dataclass
class DependencyStatus:
    name: str
    status: str
    detail: str = ""


@dataclass
class HealthSnapshot:
    liveness: str
    readiness: str
    role: str
    draining: bool = False
    dependencies: list[DependencyStatus] = field(default_factory=list)
    metrics_preview: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "liveness": self.liveness,
            "readiness": self.readiness,
            "role": self.role,
            "draining": self.draining,
            "dependencies": [
                {"name": d.name, "status": d.status, "detail": d.detail}
                for d in self.dependencies
            ],
            "metrics_preview": dict(self.metrics_preview),
            "capabilities": dict(self.capabilities),
            "checked_at": self.checked_at,
        }


class DrainController:
    """Process-local drain flag for API admission + worker claims."""

    def __init__(self):
        self._draining = False
        self._started_at: float | None = None

    @property
    def draining(self) -> bool:
        return self._draining

    def begin_drain(self) -> None:
        if not self._draining:
            self._draining = True
            self._started_at = time.time()

    def clear_drain(self) -> None:
        self._draining = False
        self._started_at = None

    def age_seconds(self) -> float | None:
        if self._started_at is None:
            return None
        return max(0.0, time.time() - self._started_at)


# Process singleton used by HTTP + compose.
DRAIN = DrainController()


def _check_store(name: str, store, *, required: bool = True) -> DependencyStatus:
    if store is None:
        return DependencyStatus(
            name,
            STATUS_NOT_READY if required else STATUS_DEGRADED,
            "missing",
        )
    # Prefer explicit available/ready flags when present.
    if hasattr(store, "available") and store.available is False:
        return DependencyStatus(name, STATUS_NOT_READY, "unavailable")
    try:
        # Cheap touch: list/get schema without network AI calls.
        if hasattr(store, "list_all"):
            store.list_all()
        elif hasattr(store, "diagnostics"):
            store.diagnostics()
        elif hasattr(store, "get"):
            store.get("__health_probe_missing__")
        return DependencyStatus(name, STATUS_HEALTHY, "ok")
    except Exception as exc:
        return DependencyStatus(name, STATUS_NOT_READY, type(exc).__name__)


def evaluate_readiness(
    *,
    side_effect_runtime=None,
    runtime_config: RuntimeConfig | None = None,
    env: Mapping | None = None,
    draining: bool | None = None,
    health_tracker=None,
    runtime_stats=None,
) -> HealthSnapshot:
    """Bounded readiness for API / WORKER / COMBINED without provider HTTP calls.

    Routing provider-health and runtime-stats are process-local today. Shared
    cross-worker backing is reported under ``capabilities`` and as informational
    HEALTHY dependency details — it does not fail liveness or ordinary readiness
    for single-process / supported deployments that accept per-worker isolation.
    """

    cfg = runtime_config or validate_runtime_config(env, raise_on_error=False)
    role = cfg.runtime_role
    is_draining = DRAIN.draining if draining is None else bool(draining)
    deps: list[DependencyStatus] = []

    from agents.routing_state_scope import (
        SHARED_BACKING_NOT_AVAILABLE,
        STATE_SCOPE_PROCESS_LOCAL,
        routing_coordination_capabilities,
    )

    caps = routing_coordination_capabilities(
        health_tracker=health_tracker,
        runtime_stats=runtime_stats,
    )
    # Informational only (STATUS_HEALTHY): do not fail single-process readiness
    # solely because shared routing health is unimplemented.
    deps.append(
        DependencyStatus(
            "routing_provider_health",
            STATUS_HEALTHY,
            f"scope={caps.get('routing_health_scope', STATE_SCOPE_PROCESS_LOCAL)};"
            f"shared_backing={caps.get('routing_health_shared_backing', SHARED_BACKING_NOT_AVAILABLE)}",
        )
    )
    deps.append(
        DependencyStatus(
            "routing_runtime_stats",
            STATUS_HEALTHY,
            f"scope={caps.get('routing_runtime_stats_scope', STATE_SCOPE_PROCESS_LOCAL)};"
            f"shared_backing={caps.get('routing_runtime_stats_shared_backing', SHARED_BACKING_NOT_AVAILABLE)}",
        )
    )

    if cfg.errors:
        deps.append(
            DependencyStatus(
                "runtime_config",
                STATUS_NOT_READY,
                ",".join(cfg.errors),
            )
        )
    else:
        deps.append(DependencyStatus("runtime_config", STATUS_HEALTHY, cfg.profile))

    try:
        from production_foundation.config import validate_production_config, resolve_environment

        pf_report = validate_production_config(env)
        if resolve_environment(env) == "production" and pf_report.overall == "FAIL":
            deps.append(DependencyStatus("production_config", STATUS_NOT_READY, pf_report.overall))
        else:
            deps.append(DependencyStatus("production_config", STATUS_HEALTHY, pf_report.overall))
    except Exception as exc:
        deps.append(DependencyStatus("production_config", STATUS_DEGRADED, type(exc).__name__))

    persistence = getattr(side_effect_runtime, "persistence", None) if side_effect_runtime else None
    wr = getattr(side_effect_runtime, "workflow_runtime", None) if side_effect_runtime else None

    if persistence is None:
        deps.append(DependencyStatus("persistence", STATUS_NOT_READY, "missing"))
    else:
        if persistence.ready:
            deps.append(
                DependencyStatus(
                    "persistence",
                    STATUS_HEALTHY,
                    f"{persistence.backend}:v{persistence.schema_version}",
                )
            )
        else:
            deps.append(
                DependencyStatus(
                    "persistence",
                    STATUS_NOT_READY,
                    getattr(persistence, "reason_code", None) or "not_ready",
                )
            )
        deps.append(
            _check_store(
                "workflow_store",
                getattr(persistence, "workflow_runtime_store", None),
                required=True,
            )
        )
        deps.append(
            _check_store(
                "queue_store",
                getattr(persistence, "task_queue_store", None),
                required=True,
            )
        )
        deps.append(
            _check_store(
                "schedule_store",
                getattr(persistence, "schedule_store", None),
                required=role_runs_worker_loops(role),
            )
        )

    # Budget / governor are soft-degraded when missing in API (admission still works).
    budget = None
    governor = None
    if side_effect_runtime is not None:
        # Wired on router in main; optional on runtime attrs if set.
        budget = getattr(side_effect_runtime, "budget_store", None)
        governor = getattr(side_effect_runtime, "provider_governor", None)

    if role in {ROLE_API, ROLE_COMBINED}:
        if budget is not None:
            deps.append(_check_store("budget_store", budget, required=False))
        else:
            deps.append(DependencyStatus("budget_store", STATUS_DEGRADED, "not_wired"))

    if role_runs_worker_loops(role):
        if wr is None:
            deps.append(DependencyStatus("workflow_runtime", STATUS_NOT_READY, "missing"))
        else:
            defs = getattr(wr, "definitions", None)
            if defs is None:
                deps.append(
                    DependencyStatus("definition_registry", STATUS_NOT_READY, "missing")
                )
            else:
                deps.append(
                    DependencyStatus("definition_registry", STATUS_HEALTHY, "ok")
                )
            if getattr(wr, "_claims_stopped", False) and not is_draining:
                deps.append(
                    DependencyStatus("worker_claims", STATUS_DEGRADED, "claims_stopped")
                )
            else:
                deps.append(DependencyStatus("worker_claims", STATUS_HEALTHY, "ok"))
        if governor is not None:
            store = getattr(governor, "store", governor)
            deps.append(_check_store("provider_governor", store, required=False))
        else:
            deps.append(
                DependencyStatus("provider_governor", STATUS_DEGRADED, "not_wired")
            )

    statuses = {d.status for d in deps}
    if STATUS_NOT_READY in statuses or is_draining:
        readiness = STATUS_NOT_READY if is_draining or STATUS_NOT_READY in statuses else STATUS_DEGRADED
        if is_draining:
            readiness = STATUS_NOT_READY
    elif STATUS_DEGRADED in statuses:
        readiness = STATUS_DEGRADED
    else:
        readiness = STATUS_HEALTHY

    return HealthSnapshot(
        liveness=STATUS_HEALTHY,
        readiness=readiness,
        role=role,
        draining=is_draining,
        dependencies=deps,
        capabilities=caps,
    )


def begin_api_drain(*, workflow_runtime=None) -> None:
    """Mark API not ready; stop new admission. In-flight HTTP may finish."""

    DRAIN.begin_drain()
    if workflow_runtime is not None and hasattr(workflow_runtime, "stop_new_claims"):
        # API-only should not run claims; still safe to set the flag.
        if getattr(workflow_runtime, "runtime_role", None) in {
            ROLE_API,
            ROLE_COMBINED,
            ROLE_WORKER,
        }:
            pass


async def begin_worker_drain(
    workflow_runtime, *, wait_seconds: float = 5.0
) -> None:
    """Stop new claims/schedule ticks; bounded wait then stop background loop."""

    DRAIN.begin_drain()
    if workflow_runtime is None:
        return
    if hasattr(workflow_runtime, "stop_new_claims"):
        workflow_runtime.stop_new_claims()
    import asyncio

    await asyncio.sleep(max(0.0, float(wait_seconds)))
    if hasattr(workflow_runtime, "stop_background"):
        await workflow_runtime.stop_background()
