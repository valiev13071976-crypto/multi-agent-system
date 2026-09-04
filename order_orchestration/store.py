"""Tenant-scoped Panda order store + audit. Reuses autonomy idempotency."""

from __future__ import annotations

from autonomy.models import IDEMPOTENCY_COMPLETED, IdempotencyRecord, utc_now
from autonomy.store import InMemoryIdempotencyStore
from security.tenant import require_tenant_id

from order_orchestration.contracts import CanonicalOrder
from order_orchestration.errors import ORDER_ACCESS_DENIED, OrderOrchError


class OrderStore:
    def __init__(self, idempotency: InMemoryIdempotencyStore | None = None) -> None:
        self._orders: dict[str, dict[str, CanonicalOrder]] = {}
        self._by_ext: dict[str, dict[tuple[str, str], str]] = {}
        self._audit: dict[str, list[dict]] = {}
        self._idem = idempotency or InMemoryIdempotencyStore()
        self._downstream: dict[str, dict[str, dict]] = {}

    def save(self, order: CanonicalOrder) -> CanonicalOrder:
        tenant = require_tenant_id(order.tenant_id)
        self._orders.setdefault(tenant, {})[order.order_id] = order
        self._by_ext.setdefault(tenant, {})[(order.source, order.external_order_id)] = order.order_id
        return order

    def get(self, order_id: str, *, tenant_id: str) -> CanonicalOrder:
        tenant = require_tenant_id(tenant_id)
        order = self._orders.get(tenant, {}).get(order_id)
        if order is None:
            for other, items in self._orders.items():
                if other != tenant and order_id in items:
                    raise OrderOrchError(ORDER_ACCESS_DENIED)
            raise OrderOrchError(ORDER_ACCESS_DENIED, "not_found")
        return order

    def by_external(self, *, tenant_id: str, source: str, external_order_id: str) -> CanonicalOrder | None:
        tenant = require_tenant_id(tenant_id)
        oid = self._by_ext.get(tenant, {}).get((source, external_order_id))
        if not oid:
            return None
        return self._orders.get(tenant, {}).get(oid)

    def audit(self, tenant_id: str, event: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        safe = {k: v for k, v in event.items() if k not in {"phone", "email", "address", "name", "full_name"}}
        self._audit.setdefault(tenant, []).append(safe)

    def list_audit(self, *, tenant_id: str) -> list[dict]:
        return list(self._audit.get(require_tenant_id(tenant_id), []))

    def remember(self, *, key: str, execution_id: str) -> None:
        now = utc_now()
        self._idem.put(
            IdempotencyRecord(key=key, action_id=key, state=IDEMPOTENCY_COMPLETED, created_at=now, updated_at=now, execution_id=execution_id)
        )

    def completed(self, key: str) -> IdempotencyRecord | None:
        rec = self._idem.get(key)
        if rec and rec.state == IDEMPOTENCY_COMPLETED:
            return rec
        return None

    def save_downstream(self, *, tenant_id: str, key: str, payload: dict) -> None:
        self._downstream.setdefault(require_tenant_id(tenant_id), {})[key] = dict(payload)

    def get_downstream(self, *, tenant_id: str, key: str) -> dict | None:
        return self._downstream.get(require_tenant_id(tenant_id), {}).get(key)
