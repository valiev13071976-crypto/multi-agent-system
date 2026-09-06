"""System-level load shedding under overload (Scale 3.32).

Distinct from workflow.admission (3.17/3.19), which protects INDIVIDUAL
queue/tenant capacity (pending/running counts against configured quotas).
This module protects the OVERALL runtime once continued admission becomes
unsafe, using trusted, already-computed runtime signals (queue/pool
saturation, oldest-queued age, provider governor saturation, tenant quota
state) -- never user-controlled request arguments.

Decision codes are intentionally distinct from admission's ACCEPT/REJECT/
DEFER to avoid conflating the two policies:

    ACCEPT            - proceed to normal admission/enqueue.
    DEFER             - not unsafe, but caller should back off briefly
                        (e.g. retry admission shortly) without rejecting.
    REJECT_RETRYABLE  - reject now; caller may safely retry later (durable
                        work is never silently dropped -- callers must NOT
                        enqueue on this decision, but the caller's own
                        already-accepted/durable state is untouched).
    SHED              - protect the runtime: new (typically heavy/
                        background) admission is shed outright while
                        latency-sensitive/interactive work remains
                        protected.

Interactive/critical workload is never shed by this policy: only
BATCH/BACKGROUND pressure is deterministically shed/deferred, per the
required priority principle (protect latency-sensitive execution first).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from task_queue.lanes import LANE_INTERACTIVE, is_interactive_lane

DECISION_ACCEPT = "ACCEPT"
DECISION_DEFER = "DEFER"
DECISION_REJECT_RETRYABLE = "REJECT_RETRYABLE"
DECISION_SHED = "SHED"

_VALID_DECISIONS = frozenset(
    {DECISION_ACCEPT, DECISION_DEFER, DECISION_REJECT_RETRYABLE, DECISION_SHED}
)


@dataclass(frozen=True)
class LoadShedConfig:
    """Bounded, typed, safe-default thresholds for system-level shedding."""

    enabled: bool = False
    pool_saturation_shed_threshold: float = 0.98
    pool_saturation_defer_threshold: float = 0.90
    oldest_queued_age_shed_seconds: float = 300.0
    oldest_queued_age_defer_seconds: float = 60.0
    provider_saturation_shed: bool = True

    @classmethod
    def from_env(cls, env: Mapping | None = None) -> "LoadShedConfig":
        source = env if env is not None else os.environ

        def _bool(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

        def _ratio(name: str, default: float) -> float:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                value = float(str(raw).strip())
            except ValueError:
                return default
            if value < 0.0 or value > 1.0:
                return default
            return value

        def _seconds(name: str, default: float) -> float:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                value = float(str(raw).strip())
            except ValueError:
                return default
            return value if value > 0 else default

        return cls(
            enabled=_bool("LOAD_SHED_ENABLED", False),
            pool_saturation_shed_threshold=_ratio(
                "LOAD_SHED_POOL_SATURATION_SHED", 0.98
            ),
            pool_saturation_defer_threshold=_ratio(
                "LOAD_SHED_POOL_SATURATION_DEFER", 0.90
            ),
            oldest_queued_age_shed_seconds=_seconds(
                "LOAD_SHED_OLDEST_AGE_SHED_SECONDS", 300.0
            ),
            oldest_queued_age_defer_seconds=_seconds(
                "LOAD_SHED_OLDEST_AGE_DEFER_SECONDS", 60.0
            ),
            provider_saturation_shed=_bool("LOAD_SHED_ON_PROVIDER_SATURATION", True),
        )


@dataclass(frozen=True)
class LoadSignals:
    """Trusted runtime signals used for the shedding decision.

    Never sourced from request/tool arguments or message text -- callers
    must build this from server-side runtime state (pool concurrency
    snapshots, queue depth counters, provider governor breaker state).
    """

    execution_lane: str = "background"
    pool_saturation: float = 0.0  # active / max_concurrency, 0..1
    oldest_queued_age_seconds: float = 0.0
    provider_saturated: bool = False
    tenant_quota_exhausted: bool = False


@dataclass(frozen=True)
class LoadShedDecision:
    decision: str
    reason_code: str
    metadata: Mapping[str, object] | None = None

    def __post_init__(self):
        if self.decision not in _VALID_DECISIONS:
            raise ValueError(f"invalid load-shed decision: {self.decision!r}")


class LoadShedRejectedError(Exception):
    """Raised by ``enforce_load_shedding`` for SHED/REJECT_RETRYABLE decisions.

    Callers that catch this must NOT silently drop already-accepted durable
    work -- it is only ever raised before a new admission/enqueue, never
    after a task is already durably queued.
    """

    def __init__(self, decision: LoadShedDecision):
        self.decision = decision.decision
        self.reason_code = decision.reason_code
        self.metadata = dict(decision.metadata or {})
        super().__init__(f"{decision.decision}:{decision.reason_code}")


def _record_metrics(decision: LoadShedDecision) -> None:
    if decision.decision == DECISION_ACCEPT:
        return
    try:
        from runtime.metrics import RUNTIME_COUNTERS

        lane = str((decision.metadata or {}).get("execution_lane") or "")
        RUNTIME_COUNTERS.inc("load_shed", lane=lane)
    except Exception:
        pass


def evaluate_load_shedding(
    signals: LoadSignals, config: LoadShedConfig | None = None
) -> LoadShedDecision:
    """Deterministic, pure decision function (Scale 3.32).

    Interactive/critical lanes are never shed or deferred by this policy --
    they always ACCEPT here (system-level protection targets
    batch/background pressure; interactive admission is still separately
    governed by workflow.admission's own reserve/limits).
    """

    cfg = config or LoadShedConfig()
    if not cfg.enabled:
        return LoadShedDecision(DECISION_ACCEPT, "load_shed_disabled")

    if is_interactive_lane(signals.execution_lane) or signals.execution_lane == LANE_INTERACTIVE:
        return LoadShedDecision(
            DECISION_ACCEPT,
            "interactive_protected",
            metadata={"execution_lane": signals.execution_lane},
        )

    meta = {
        "execution_lane": signals.execution_lane,
        "pool_saturation": signals.pool_saturation,
        "oldest_queued_age_seconds": signals.oldest_queued_age_seconds,
        "provider_saturated": signals.provider_saturated,
    }

    decision: LoadShedDecision
    if signals.provider_saturated and cfg.provider_saturation_shed:
        decision = LoadShedDecision(DECISION_SHED, "provider_saturated", metadata=meta)
    elif signals.pool_saturation >= cfg.pool_saturation_shed_threshold:
        decision = LoadShedDecision(DECISION_SHED, "pool_saturated", metadata=meta)
    elif signals.oldest_queued_age_seconds >= cfg.oldest_queued_age_shed_seconds:
        decision = LoadShedDecision(DECISION_SHED, "oldest_queued_age_exceeded", metadata=meta)
    elif signals.pool_saturation >= cfg.pool_saturation_defer_threshold:
        decision = LoadShedDecision(DECISION_DEFER, "pool_saturation_elevated", metadata=meta)
    elif signals.oldest_queued_age_seconds >= cfg.oldest_queued_age_defer_seconds:
        decision = LoadShedDecision(DECISION_DEFER, "oldest_queued_age_elevated", metadata=meta)
    elif signals.tenant_quota_exhausted:
        # System-level shedding never grants a bypass for an already-exhausted
        # tenant quota; existing tenant admission (3.17/3.19) governs this --
        # this policy simply declines to grant an exception under pressure.
        decision = LoadShedDecision(
            DECISION_REJECT_RETRYABLE, "tenant_quota_exhausted", metadata=meta
        )
    else:
        decision = LoadShedDecision(DECISION_ACCEPT, "accepted", metadata=meta)

    _record_metrics(decision)
    return decision


def enforce_load_shedding(
    signals: LoadSignals, config: LoadShedConfig | None = None
) -> LoadShedDecision:
    """Evaluate and raise ``LoadShedRejectedError`` on SHED/REJECT_RETRYABLE.

    DEFER and ACCEPT are returned normally (the caller decides how to
    back off on DEFER; it is not a hard rejection)."""

    decision = evaluate_load_shedding(signals, config)
    if decision.decision in {DECISION_SHED, DECISION_REJECT_RETRYABLE}:
        raise LoadShedRejectedError(decision)
    return decision


def build_load_signals(
    *,
    execution_lane: str = "background",
    pool_concurrency: Mapping[str, object] | None = None,
    oldest_queued_age_seconds: float | None = None,
    provider_saturated: bool = False,
    tenant_quota_exhausted: bool = False,
) -> LoadSignals:
    """Convenience builder from already-computed runtime snapshots (e.g.
    ``WorkflowRuntimeBundle.concurrency_snapshot()`` and
    ``observability.runtime_metrics.collect_queue_snapshot``). Never reads
    request/tool arguments."""

    pc = dict(pool_concurrency or {})
    max_c = int(pc.get("max_concurrency") or 0)
    active = int(pc.get("active") or 0)
    saturation = (active / max_c) if max_c > 0 else 0.0
    return LoadSignals(
        execution_lane=execution_lane,
        pool_saturation=max(0.0, min(1.0, saturation)),
        oldest_queued_age_seconds=float(oldest_queued_age_seconds or 0.0),
        provider_saturated=bool(provider_saturated),
        tenant_quota_exhausted=bool(tenant_quota_exhausted),
    )
