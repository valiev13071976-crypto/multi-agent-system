"""Deterministic operational health snapshots (no AI, no network)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping


HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_BLOCKED = "blocked"
HEALTH_STATUSES = (HEALTH_HEALTHY, HEALTH_DEGRADED, HEALTH_BLOCKED)


@dataclass(frozen=True)
class OperationalHealthSnapshot:
    timestamp: datetime
    overall_status: str
    workflow_status: str = HEALTH_HEALTHY
    tool_gateway_status: str = HEALTH_HEALTHY
    persistence_status: str = HEALTH_HEALTHY
    protected_state_status: str = HEALTH_HEALTHY
    queue_status: str = HEALTH_HEALTHY
    provider_status: str = HEALTH_HEALTHY
    finops_status: str = HEALTH_HEALTHY
    reconciliation_status: str = HEALTH_HEALTHY
    recovery_status: str = HEALTH_HEALTHY
    memory_status: str = HEALTH_HEALTHY
    document_status: str = HEALTH_HEALTHY
    knowledge_status: str = HEALTH_HEALTHY
    procurement_status: str = HEALTH_HEALTHY
    active_workflows: int = 0
    waiting_approval: int = 0
    uncertain_side_effects: int = 0
    pending_reconciliations: int = 0
    open_recovery_cases: int = 0
    pending_manual_review: int = 0
    stale_recovery_jobs: int = 0
    dead_letter_count: int = 0
    tool_failures_recent: int = 0
    provider_failures_recent: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.overall_status not in HEALTH_STATUSES:
            raise ValueError("invalid_health_status")
        stamp = self.timestamp
        if stamp.tzinfo is None:
            object.__setattr__(
                self, "timestamp", stamp.replace(tzinfo=timezone.utc)
            )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata or {})))


def _worst(*statuses: str) -> str:
    order = {HEALTH_HEALTHY: 0, HEALTH_DEGRADED: 1, HEALTH_BLOCKED: 2}
    return max(statuses, key=lambda s: order.get(s, 0))


def build_operational_health(
    *,
    persistence_ready: bool = True,
    protected_state_ready: bool = True,
    protected_write_required: bool = False,
    active_workflows: int = 0,
    waiting_approval: int = 0,
    uncertain_side_effects: int = 0,
    pending_reconciliations: int = 0,
    dead_letter_count: int = 0,
    tool_failures_recent: int = 0,
    provider_failures_recent: int = 0,
    dead_letter_threshold: int = 10,
    finops_status: str = HEALTH_HEALTHY,
    budget_persistence_ready: bool = True,
    budget_enforcement_required: bool = False,
    budget_soft_threshold: bool = False,
    budget_uncertain: bool = False,
    budget_hard_exhausted: bool = False,
    open_recovery_cases: int = 0,
    pending_manual_review: int = 0,
    stale_recovery_jobs: int = 0,
    critical_recovery_blocking: bool = False,
    recovery_persistence_ready: bool = True,
    recovery_required: bool = False,
    memory_status: str = HEALTH_HEALTHY,
    memory_enabled: bool = False,
    memory_persistence_ready: bool = True,
    document_status: str = HEALTH_HEALTHY,
    documents_enabled: bool = False,
    document_persistence_ready: bool = True,
    knowledge_status: str = HEALTH_HEALTHY,
    knowledge_enabled: bool = False,
    knowledge_persistence_ready: bool = True,
    procurement_status: str = HEALTH_HEALTHY,
    procurement_enabled: bool = False,
    procurement_persistence_ready: bool = True,
    now: datetime | None = None,
) -> OperationalHealthSnapshot:
    persistence_status = HEALTH_HEALTHY if persistence_ready else HEALTH_DEGRADED
    protected_status = HEALTH_HEALTHY if protected_state_ready else HEALTH_DEGRADED
    if protected_write_required and not protected_state_ready:
        protected_status = HEALTH_BLOCKED
        persistence_status = HEALTH_BLOCKED

    workflow_status = HEALTH_HEALTHY
    if waiting_approval > 0:
        workflow_status = HEALTH_DEGRADED

    tool_status = HEALTH_HEALTHY
    if tool_failures_recent > 0:
        tool_status = HEALTH_DEGRADED

    queue_status = HEALTH_HEALTHY
    if dead_letter_count >= dead_letter_threshold:
        queue_status = HEALTH_DEGRADED

    provider_status = HEALTH_HEALTHY
    if provider_failures_recent > 0:
        provider_status = HEALTH_DEGRADED

    recon_status = HEALTH_HEALTHY
    if uncertain_side_effects > 0 or pending_reconciliations > 0:
        recon_status = HEALTH_DEGRADED

    recovery_status = HEALTH_HEALTHY
    if open_recovery_cases > 0 or pending_manual_review > 0 or stale_recovery_jobs > 0:
        recovery_status = HEALTH_DEGRADED
    if critical_recovery_blocking or (recovery_required and not recovery_persistence_ready):
        recovery_status = HEALTH_BLOCKED

    mem_status = memory_status if memory_status in HEALTH_STATUSES else HEALTH_HEALTHY
    if memory_enabled and not memory_persistence_ready:
        mem_status = HEALTH_BLOCKED

    doc_status = document_status if document_status in HEALTH_STATUSES else HEALTH_HEALTHY
    if documents_enabled and not document_persistence_ready:
        doc_status = HEALTH_BLOCKED

    know_status = knowledge_status if knowledge_status in HEALTH_STATUSES else HEALTH_HEALTHY
    if knowledge_enabled and not knowledge_persistence_ready:
        know_status = HEALTH_BLOCKED

    proc_status = procurement_status if procurement_status in HEALTH_STATUSES else HEALTH_HEALTHY
    if procurement_enabled and not procurement_persistence_ready:
        proc_status = HEALTH_BLOCKED

    finops = finops_status if finops_status in HEALTH_STATUSES else HEALTH_HEALTHY
    if budget_soft_threshold or budget_uncertain:
        finops = _worst(finops, HEALTH_DEGRADED)
    if budget_enforcement_required and not budget_persistence_ready:
        finops = HEALTH_BLOCKED
    if budget_hard_exhausted:
        finops = HEALTH_BLOCKED

    overall = _worst(
        persistence_status,
        protected_status,
        workflow_status,
        tool_status,
        queue_status,
        provider_status,
        recon_status,
        recovery_status,
        mem_status,
        doc_status,
        know_status,
        proc_status,
        finops,
        HEALTH_HEALTHY,
    )
    return OperationalHealthSnapshot(
        timestamp=now or datetime.now(timezone.utc),
        overall_status=overall,
        workflow_status=workflow_status,
        tool_gateway_status=tool_status,
        persistence_status=persistence_status,
        protected_state_status=protected_status,
        queue_status=queue_status,
        provider_status=provider_status,
        finops_status=finops,
        reconciliation_status=recon_status,
        recovery_status=recovery_status,
        memory_status=mem_status,
        document_status=doc_status,
        knowledge_status=know_status,
        procurement_status=proc_status,
        active_workflows=int(active_workflows),
        waiting_approval=int(waiting_approval),
        uncertain_side_effects=int(uncertain_side_effects),
        pending_reconciliations=int(pending_reconciliations),
        open_recovery_cases=int(open_recovery_cases),
        pending_manual_review=int(pending_manual_review),
        stale_recovery_jobs=int(stale_recovery_jobs),
        dead_letter_count=int(dead_letter_count),
        tool_failures_recent=int(tool_failures_recent),
        provider_failures_recent=int(provider_failures_recent),
    )
