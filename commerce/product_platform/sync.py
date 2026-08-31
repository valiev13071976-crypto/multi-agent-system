"""Commerce sync planning + circular sync loop prevention."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from commerce.product_platform.errors import COMMERCE_SYNC_LOOP_TERMINATED, ProductPlatformError
from commerce.product_platform.models import CommerceSyncPlan, SyncConflict
from commerce.product_platform.ownership import detect_sync_conflict


@dataclass
class SyncEventLedger:
    """Tracks outbound causation so reflected inbound events terminate."""

    _outbound: dict[str, str] = field(default_factory=dict)  # causation_id -> plan_id
    _acked: set[str] = field(default_factory=set)

    def record_outbound(self, *, causation_id: str, plan_id: str) -> None:
        self._outbound[causation_id] = plan_id

    def acknowledge_inbound(self, *, causation_id: str, origin: str) -> dict:
        if origin == "panda" or causation_id in self._outbound or causation_id in self._acked:
            self._acked.add(causation_id)
            return {
                "status": "ACKNOWLEDGED",
                "code": COMMERCE_SYNC_LOOP_TERMINATED,
                "terminated": True,
                "reason": "panda_origin_reflected",
            }
        return {"status": "PROCESS", "terminated": False}


def plan_sync(
    *,
    tenant_id: str,
    integration: str,
    direction: str,
    changes: list[dict],
    policy,
    dry_run: bool = True,
    causation_id: str = "",
) -> CommerceSyncPlan:
    conflicts: list[SyncConflict] = []
    accepted: list[dict] = []
    skipped = 0
    for change in changes:
        field = str(change.get("field") or "")
        conflict = detect_sync_conflict(
            tenant_id=tenant_id,
            entity_type=str(change.get("entity_type") or "product"),
            entity_id=str(change.get("entity_id") or ""),
            field=field,
            canonical_value=str(change.get("canonical_value") or ""),
            external_value=str(change.get("external_value") or ""),
            policy=policy,
        )
        if conflict is not None and direction in {"PUSH", "BIDIRECTIONAL"}:
            # Silent last-write-wins forbidden for critical fields
            if field in {"stock", "retail_price", "sku", "order_status", "payment_status"}:
                conflicts.append(conflict)
                skipped += 1
                continue
        accepted.append(dict(change))
    return CommerceSyncPlan(
        plan_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        integration=integration,
        direction=direction,
        dry_run=dry_run,
        changes=tuple(accepted),
        conflicts=tuple(conflicts),
        skipped=skipped,
        idempotency_key=causation_id or str(uuid.uuid4()),
    )


def assert_not_loop(ledger: SyncEventLedger, *, causation_id: str, origin: str) -> None:
    result = ledger.acknowledge_inbound(causation_id=causation_id, origin=origin)
    if result.get("terminated"):
        raise ProductPlatformError(COMMERCE_SYNC_LOOP_TERMINATED, "reflected_panda_event")
