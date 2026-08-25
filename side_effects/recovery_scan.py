"""Local-only recovery candidate scan. Never calls network or mutates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import (
    IDEMPOTENCY_STARTED,
    IDEMPOTENCY_UNCERTAIN,
    sanitize_metadata,
    utc_now,
)
from side_effects.models import STATUS_UNKNOWN, STATUS_SUCCEEDED


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
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))

    def as_dict(self) -> dict:
        return {
            "stale_started_count": self.stale_started_count,
            "uncertain_count": self.uncertain_count,
            "pending_reconciliation_count": self.pending_reconciliation_count,
            "manual_review_count": self.manual_review_count,
            "last_scan_at": self.last_scan_at.isoformat(),
            "network_calls": self.network_calls,
            "mutation_calls": self.mutation_calls,
        }


def scan_recovery_candidates(
    *,
    execution_store,
    idempotency_store=None,
    reconciliation_store=None,
    now=None,
) -> RecoveryScanResult:
    """Discover local candidates only. No GitHub, no retry, no rollback."""

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
    return RecoveryScanResult(
        stale_started_count=int(stale_started),
        uncertain_count=int(uncertain),
        pending_reconciliation_count=int(pending),
        manual_review_count=int(manual),
        last_scan_at=stamp,
        network_calls=0,
        mutation_calls=0,
    )
