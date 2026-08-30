"""Phase 3 Block 3 — production runtime configuration profiles + fail-fast validation.

Does not replace per-module from_env readers; validates coherent combinations once
at compose/startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from providers.governor import GovernorLimits
from task_queue.lanes import (
    EXECUTION_LANES,
    LaneCapacityConfig,
    parse_worker_lanes,
)
from workflow.admission import AdmissionLimits
from workflow.runtime_role import (
    ROLE_API,
    ROLE_COMBINED,
    ROLE_WORKER,
    resolve_runtime_role,
    role_runs_worker_loops,
)


class RuntimeConfigError(ValueError):
    """Fail-fast invalid production configuration."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("invalid_runtime_config:" + ";".join(self.errors))


PROFILE_DEVELOPMENT = "development"
PROFILE_SINGLE_NODE = "single-node-production"
PROFILE_MULTI_PROCESS = "multi-process-production"
PROFILES = frozenset(
    {PROFILE_DEVELOPMENT, PROFILE_SINGLE_NODE, PROFILE_MULTI_PROCESS}
)

# Documented safe defaults per profile (env overrides still win after apply).
PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    PROFILE_DEVELOPMENT: {
        "RUNTIME_ROLE": "combined",
        "WORKER_LANES": "all",
        "MAX_PENDING_GLOBAL": "1000",
        "MAX_PENDING_PER_TENANT": "100",
        "MAX_RUNNING_GLOBAL": "20",
        "MAX_RUNNING_PER_TENANT": "10",
        "INTERACTIVE_RESERVED": "5",
        "INTERACTIVE_PENDING_RESERVE": "50",
        "BACKGROUND_MAY_BORROW_INTERACTIVE": "true",
        "WORKER_LEASE_SECONDS": "300",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS": "30",
        "WORKER_POLL_INTERVAL_SECONDS": "0.25",
        "SCHEDULE_CLAIM_LEASE_SECONDS": "120",
        "PROVIDER_GOVERNOR_ENABLED": "true",
        "PROVIDER_MAX_CONCURRENCY": "8",
        "PROVIDER_INTERACTIVE_RESERVED": "2",
        "PROVIDER_BREAKER_FAILURE_THRESHOLD": "5",
        "PROVIDER_BREAKER_COOLDOWN_SECONDS": "30",
        "PROVIDER_BREAKER_HALF_OPEN_PROBES": "1",
        "PROVIDER_SLOT_TTL_SECONDS": "120",
        "TENANT_FAIRNESS_ENABLED": "true",
        "PRIORITY_AGING_SECONDS": "60",
        "PRIORITY_AGING_MAX_BOOST": "2",
    },
    PROFILE_SINGLE_NODE: {
        "RUNTIME_ROLE": "combined",
        "WORKER_LANES": "all",
        "MAX_PENDING_GLOBAL": "2000",
        "MAX_PENDING_PER_TENANT": "200",
        "MAX_RUNNING_GLOBAL": "40",
        "MAX_RUNNING_PER_TENANT": "15",
        "INTERACTIVE_RESERVED": "10",
        "INTERACTIVE_PENDING_RESERVE": "100",
        "BACKGROUND_MAY_BORROW_INTERACTIVE": "true",
        "WORKER_LEASE_SECONDS": "300",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS": "30",
        "WORKER_POLL_INTERVAL_SECONDS": "0.25",
        "SCHEDULE_CLAIM_LEASE_SECONDS": "120",
        "PROVIDER_GOVERNOR_ENABLED": "true",
        "PROVIDER_MAX_CONCURRENCY": "16",
        "PROVIDER_INTERACTIVE_RESERVED": "4",
        "PROVIDER_BREAKER_FAILURE_THRESHOLD": "5",
        "PROVIDER_BREAKER_COOLDOWN_SECONDS": "60",
        "PROVIDER_BREAKER_HALF_OPEN_PROBES": "1",
        "PROVIDER_SLOT_TTL_SECONDS": "180",
        "TENANT_FAIRNESS_ENABLED": "true",
    },
    PROFILE_MULTI_PROCESS: {
        # Role/lanes intentionally set per process; defaults favour API-safe bounds.
        "RUNTIME_ROLE": "api",
        "WORKER_LANES": "all",
        "MAX_PENDING_GLOBAL": "5000",
        "MAX_PENDING_PER_TENANT": "500",
        "MAX_RUNNING_GLOBAL": "80",
        "MAX_RUNNING_PER_TENANT": "20",
        "INTERACTIVE_RESERVED": "20",
        "INTERACTIVE_PENDING_RESERVE": "200",
        "BACKGROUND_MAY_BORROW_INTERACTIVE": "true",
        "WORKER_LEASE_SECONDS": "300",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS": "30",
        "WORKER_POLL_INTERVAL_SECONDS": "0.2",
        "SCHEDULE_CLAIM_LEASE_SECONDS": "120",
        "PROVIDER_GOVERNOR_ENABLED": "true",
        "PROVIDER_MAX_CONCURRENCY": "32",
        "PROVIDER_INTERACTIVE_RESERVED": "8",
        "PROVIDER_BREAKER_FAILURE_THRESHOLD": "5",
        "PROVIDER_BREAKER_COOLDOWN_SECONDS": "60",
        "PROVIDER_BREAKER_HALF_OPEN_PROBES": "1",
        "PROVIDER_SLOT_TTL_SECONDS": "180",
        "TENANT_FAIRNESS_ENABLED": "true",
    },
}


def _env_float(source: Mapping, name: str, default: float) -> float:
    raw = source.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _env_int(source: Mapping, name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated production runtime snapshot."""

    profile: str
    runtime_role: str
    worker_lanes: frozenset[str]
    admission: AdmissionLimits
    lane_capacity: LaneCapacityConfig
    governor: GovernorLimits
    lease_seconds: float
    heartbeat_interval_seconds: float
    poll_interval_seconds: float
    schedule_claim_lease_seconds: float
    side_effect_db_path: str
    budget_db_path: str
    governor_db_path: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return not self.errors


def resolve_profile(env: Mapping | None = None) -> str:
    source = env if env is not None else os.environ
    raw = str(source.get("PANDA_RUNTIME_PROFILE") or PROFILE_DEVELOPMENT).strip().lower()
    if raw in PROFILES:
        return raw
    aliases = {
        "dev": PROFILE_DEVELOPMENT,
        "local": PROFILE_DEVELOPMENT,
        "prod": PROFILE_SINGLE_NODE,
        "production": PROFILE_SINGLE_NODE,
        "single": PROFILE_SINGLE_NODE,
        "multi": PROFILE_MULTI_PROCESS,
        "multiprocess": PROFILE_MULTI_PROCESS,
        "multi-process": PROFILE_MULTI_PROCESS,
    }
    return aliases.get(raw, PROFILE_DEVELOPMENT)


def apply_profile_defaults(env: Mapping | None = None) -> dict[str, str]:
    """Return a merged env dict: profile defaults filled only where unset."""

    source = dict(env if env is not None else os.environ)
    profile = resolve_profile(source)
    for key, value in PROFILE_DEFAULTS.get(profile, {}).items():
        if key not in source or str(source.get(key) or "").strip() == "":
            source[key] = value
    source["PANDA_RUNTIME_PROFILE"] = profile
    return source


def validate_runtime_config(
    env: Mapping | None = None, *, raise_on_error: bool = True
) -> RuntimeConfig:
    """Fail-fast validation of dangerous capacity / lease / role combinations."""

    merged = apply_profile_defaults(env)
    errors: list[str] = []
    warnings: list[str] = []
    profile = resolve_profile(merged)

    role_raw = str(merged.get("RUNTIME_ROLE") or "").strip().lower()
    if role_raw and role_raw not in {ROLE_API, ROLE_WORKER, ROLE_COMBINED}:
        # resolve_runtime_role may still map legacy flags; flag explicit garbage.
        if merged.get("WORKFLOW_WORKER_ENABLED") is None:
            errors.append(f"invalid_runtime_role:{role_raw}")
    role = resolve_runtime_role(merged)

    lanes = parse_worker_lanes(merged.get("WORKER_LANES"))
    if role_runs_worker_loops(role) and not lanes:
        errors.append("worker_lanes_empty")
    if lanes and not lanes.issubset(set(EXECUTION_LANES)):
        errors.append("worker_lanes_invalid")

    admission = AdmissionLimits.from_env(merged)
    lane_cfg = LaneCapacityConfig.from_env(merged)
    governor = GovernorLimits.from_env(merged)

    lease = _env_float(merged, "WORKER_LEASE_SECONDS", 300.0)
    heartbeat = _env_float(merged, "WORKER_HEARTBEAT_INTERVAL_SECONDS", 30.0)
    poll = _env_float(merged, "WORKER_POLL_INTERVAL_SECONDS", 0.25)
    sched_lease = _env_float(merged, "SCHEDULE_CLAIM_LEASE_SECONDS", 120.0)

    if lease <= 0:
        errors.append("lease_seconds_invalid")
    if heartbeat <= 0:
        errors.append("heartbeat_interval_invalid")
    if poll <= 0:
        errors.append("poll_interval_invalid")
    if sched_lease <= 0:
        errors.append("schedule_claim_lease_invalid")
    if heartbeat >= lease:
        errors.append("heartbeat_interval_gte_lease_duration")

    # Capacity consistency
    if admission.max_running_global is not None and admission.max_running_global <= 0:
        errors.append("max_running_global_invalid")
    if admission.max_pending_global is not None and admission.max_pending_global <= 0:
        errors.append("max_pending_global_invalid")
    if (
        admission.max_running_global is not None
        and lane_cfg.interactive_reserved > admission.max_running_global
    ):
        errors.append("interactive_reserved_gt_max_running_global")
    if (
        admission.max_running_global is not None
        and admission.max_running_per_tenant is not None
        and admission.max_running_per_tenant > admission.max_running_global
    ):
        errors.append("max_running_per_tenant_gt_max_running_global")
    if (
        admission.max_pending_global is not None
        and admission.max_pending_per_tenant is not None
        and admission.max_pending_per_tenant > admission.max_pending_global
    ):
        warnings.append("max_pending_per_tenant_gt_max_pending_global")

    if governor.enabled:
        if governor.max_concurrency <= 0:
            errors.append("provider_max_concurrency_invalid")
        if governor.interactive_reserved > governor.max_concurrency:
            errors.append("provider_interactive_reserved_gt_max_concurrency")
        if governor.failure_threshold <= 0:
            errors.append("provider_breaker_failure_threshold_invalid")
        if governor.cooldown_seconds <= 0:
            errors.append("provider_breaker_cooldown_invalid")
        if governor.half_open_probe_limit <= 0:
            errors.append("provider_breaker_half_open_probes_invalid")
        if governor.slot_ttl_seconds <= 0:
            errors.append("provider_slot_ttl_invalid")

    db = str(merged.get("SIDE_EFFECT_DB_PATH") or "./data/side_effects.sqlite3")
    budget_db = str(merged.get("FINOPS_BUDGET_DB_PATH") or db)
    gov_db = str(merged.get("PROVIDER_GOVERNOR_DB_PATH") or db)

    if profile == PROFILE_MULTI_PROCESS and role == ROLE_COMBINED:
        warnings.append("multi_process_profile_with_combined_role")

    cfg = RuntimeConfig(
        profile=profile,
        runtime_role=role,
        worker_lanes=lanes,
        admission=admission,
        lane_capacity=lane_cfg,
        governor=governor,
        lease_seconds=lease,
        heartbeat_interval_seconds=heartbeat,
        poll_interval_seconds=poll,
        schedule_claim_lease_seconds=sched_lease,
        side_effect_db_path=db,
        budget_db_path=budget_db,
        governor_db_path=gov_db,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
    if errors and raise_on_error:
        raise RuntimeConfigError(errors)
    return cfg
