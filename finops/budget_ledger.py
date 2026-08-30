"""BudgetLedger — reserved / committed / spent by scope."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from finops.budget_models import (
    ACTIVE_RESERVATION_STATUSES,
    RES_EXPIRED,
    RES_RELEASED,
    RES_RESERVED,
    SCOPE_AGENT,
    SCOPE_DAILY,
    SCOPE_GLOBAL,
    SCOPE_MODEL,
    SCOPE_MONTHLY,
    SCOPE_PROVIDER,
    SCOPE_TASK,
    SCOPE_TENANT,
    BudgetReservation,
    utc_now,
)
from finops.budget_store import BudgetStore, InMemoryBudgetStore
from finops.service import _day_bounds, _month_bounds


def scope_ref(scope: str, key: str = "") -> str:
    key = str(key or "")
    return f"{scope}:{key}" if key else f"{scope}:"


def build_scope_refs(
    *,
    task_id: str,
    agent_id: str | None,
    provider: str,
    model: str,
    when: datetime | None = None,
    tenant_id: str | None = None,
) -> tuple[str, ...]:
    stamp = when or utc_now()
    day_start, _ = _day_bounds(stamp)
    month_start, _ = _month_bounds(stamp)
    refs = [
        scope_ref(SCOPE_GLOBAL),
        scope_ref(SCOPE_TASK, task_id),
        scope_ref(SCOPE_PROVIDER, provider),
        scope_ref(SCOPE_MODEL, f"{provider}/{model}"),
        scope_ref(SCOPE_DAILY, day_start.date().isoformat()),
        scope_ref(SCOPE_MONTHLY, f"{month_start.year:04d}-{month_start.month:02d}"),
    ]
    if agent_id:
        refs.append(scope_ref(SCOPE_AGENT, agent_id))
    tid = str(tenant_id or "").strip()
    if tid:
        refs.append(scope_ref(SCOPE_TENANT, tid))
    return tuple(refs)


class BudgetLedger:
    def __init__(self, store: BudgetStore | None = None):
        self.store = store or InMemoryBudgetStore()

    def get_reserved(self, scope: str, key: str = "") -> Decimal:
        reserved, _, _ = self.store.get_totals(scope_ref(scope, key))
        return reserved

    def get_committed(self, scope: str, key: str = "") -> Decimal:
        _, committed, _ = self.store.get_totals(scope_ref(scope, key))
        return committed

    def get_spent(self, scope: str, key: str = "") -> Decimal:
        _, _, spent = self.store.get_totals(scope_ref(scope, key))
        return spent

    def get_remaining(
        self,
        *,
        hard_limit: Decimal | None,
        scope: str,
        key: str = "",
    ) -> Decimal | None:
        if hard_limit is None:
            return None
        reserved, _, spent = self.store.get_totals(scope_ref(scope, key))
        return hard_limit - (reserved + spent)

    def list_active_reservations(
        self, *, now: datetime | None = None
    ) -> tuple[BudgetReservation, ...]:
        return self.store.list_active(now=now)

    def expire_stale(self, *, now: datetime | None = None) -> tuple[BudgetReservation, ...]:
        stamp = now or utc_now()
        expired: list[BudgetReservation] = []
        for row in self.store.list_reservations():
            if row.status not in ACTIVE_RESERVATION_STATUSES:
                continue
            if row.expires_at > stamp:
                continue
            released = self.release(row.reservation_id, now=stamp, as_expired=True)
            if released is not None:
                expired.append(released)
        return tuple(expired)

    def release(
        self,
        reservation_id: str,
        *,
        now: datetime | None = None,
        as_expired: bool = False,
    ) -> BudgetReservation | None:
        stamp = now or utc_now()
        current = self.store.get_reservation(reservation_id)
        if current is None:
            return None
        if current.status in {RES_RELEASED, RES_EXPIRED}:
            return current
        if current.status not in ACTIVE_RESERVATION_STATUSES:
            return current
        for ref in current.scope_refs:
            self.store.release_reserved(ref, current.estimated_cost)
        status = RES_EXPIRED if as_expired else RES_RELEASED
        updated = BudgetReservation(
            reservation_id=current.reservation_id,
            scope_refs=current.scope_refs,
            task_id=current.task_id,
            provider=current.provider,
            model=current.model,
            estimated_cost=current.estimated_cost,
            currency=current.currency,
            status=status,
            created_at=current.created_at,
            expires_at=current.expires_at,
            agent_id=current.agent_id,
            committed_at=current.committed_at,
            released_at=stamp,
            actual_cost=current.actual_cost,
            usage_record_key=current.usage_record_key,
            metadata_safe=dict(current.metadata_safe),
            version=current.version,
        )
        return self.store.update_reservation(updated, expected_version=current.version)
