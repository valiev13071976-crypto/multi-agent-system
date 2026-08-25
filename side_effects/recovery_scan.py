"""Local-only recovery candidate scan. Never calls network or mutates externally."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import (
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    IDEMPOTENCY_STARTED,
    IDEMPOTENCY_UNCERTAIN,
    sanitize_metadata,
    utc_now,
)
from hitl.models import PERMIT_EXPIRED, PERMIT_ISSUED
from side_effects.models import STATUS_UNKNOWN, STATUS_SUCCEEDED
from workflow.models import STATUS_WAITING_APPROVAL


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value))


@dataclass(frozen=True)
class RecoveryScanResult:
    stale_started_count: int
    uncertain_count: int
    pending_reconciliation_count: int
    manual_review_count: int
    last_scan_at: datetime
    network_calls: int = 0
    mutation_calls: int = 0
    pending_approval_count: int = 0
    expired_approval_count: int = 0
    active_permit_count: int = 0
    expired_permit_count: int = 0
    waiting_approval_workflow_count: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))

    def as_dict(self) -> dict:
        return {
            "stale_started_count": self.stale_started_count,
            "uncertain_count": self.uncertain_count,
            "pending_reconciliation_count": self.pending_reconciliation_count,
            "manual_review_count": self.manual_review_count,
            "pending_approval_count": self.pending_approval_count,
            "expired_approval_count": self.expired_approval_count,
            "active_permit_count": self.active_permit_count,
            "expired_permit_count": self.expired_permit_count,
            "waiting_approval_workflow_count": self.waiting_approval_workflow_count,
            "last_scan_at": self.last_scan_at.isoformat(),
            "network_calls": self.network_calls,
            "mutation_calls": self.mutation_calls,
        }


def scan_recovery_candidates(
    *,
    execution_store,
    idempotency_store=None,
    reconciliation_store=None,
    approval_store=None,
    permit_store=None,
    workflow_runtime_store=None,
    now=None,
) -> RecoveryScanResult:
    """Discover local candidates only. No GitHub, no retry, no rollback, no approvals."""

    stamp = now or utc_now()
    stale_started = 0
    uncertain = 0
    if hasattr(execution_store, "list_all"):
        for row in execution_store.list_all():
            if row.status == STATUS_UNKNOWN or row.outcome == "uncertain":
                uncertain += 1
            elif row.status not in {
                STATUS_SUCCEEDED,
                "failed",
                "denied",
                "cancelled",
            } and row.completed_at is None:
                stale_started += 1
    if idempotency_store is not None and hasattr(idempotency_store, "list_by_state"):
        stale_started = max(
            stale_started, len(idempotency_store.list_by_state(IDEMPOTENCY_STARTED))
        )
        uncertain = max(
            uncertain, len(idempotency_store.list_by_state(IDEMPOTENCY_UNCERTAIN))
        )
    pending = 0
    manual = 0
    if reconciliation_store is not None:
        pending = len(reconciliation_store.list_pending())
        manual = len(reconciliation_store.list_manual_review())

    pending_approvals = 0
    expired_approvals = 0
    if approval_store is not None:
        if hasattr(approval_store, "list_by_status"):
            pending_approvals = len(approval_store.list_by_status(APPROVAL_PENDING))
            expired_approvals = len(approval_store.list_by_status(APPROVAL_EXPIRED))
        elif hasattr(approval_store, "list_pending"):
            pending_approvals = len(approval_store.list_pending())

    active_permits = 0
    expired_permits = 0
    if permit_store is not None and hasattr(permit_store, "list_by_status"):
        active_permits = len(permit_store.list_by_status(PERMIT_ISSUED))
        expired_permits = len(permit_store.list_by_status(PERMIT_EXPIRED))

    waiting_workflows = 0
    if workflow_runtime_store is not None:
        if hasattr(workflow_runtime_store, "list_waiting_approval"):
            waiting_workflows = len(workflow_runtime_store.list_waiting_approval())
        elif hasattr(workflow_runtime_store, "list_by_status"):
            waiting_workflows = len(
                workflow_runtime_store.list_by_status(STATUS_WAITING_APPROVAL)
            )

    return RecoveryScanResult(
        stale_started_count=int(stale_started),
        uncertain_count=int(uncertain),
        pending_reconciliation_count=int(pending),
        manual_review_count=int(manual),
        last_scan_at=stamp,
        network_calls=0,
        mutation_calls=0,
        pending_approval_count=int(pending_approvals),
        expired_approval_count=int(expired_approvals),
        active_permit_count=int(active_permits),
        expired_permit_count=int(expired_permits),
        waiting_approval_workflow_count=int(waiting_workflows),
    )
