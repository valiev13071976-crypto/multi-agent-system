"""Tenant-scoped plan/receipt/audit persistence. Reuses autonomy IdempotencyStore."""

from __future__ import annotations

from autonomy.models import IDEMPOTENCY_COMPLETED, IdempotencyRecord, utc_now
from autonomy.store import InMemoryIdempotencyStore
from security.tenant import require_tenant_id

from governed_publish.contracts import PublicationPlan, PublicationReceipt
from governed_publish.errors import PUBLISH_ACCESS_DENIED, PUBLISH_NOT_FOUND, GovernedPublishError


class GovernedPublishStore:
    def __init__(self, idempotency: InMemoryIdempotencyStore | None = None) -> None:
        self._plans: dict[str, dict[str, PublicationPlan]] = {}
        self._receipts: dict[str, dict[str, PublicationReceipt]] = {}
        self._audit: dict[str, list[dict]] = {}
        self._idem = idempotency or InMemoryIdempotencyStore()

    def save_plan(self, plan: PublicationPlan) -> PublicationPlan:
        tenant = require_tenant_id(plan.tenant_id)
        self._plans.setdefault(tenant, {})[plan.plan_id] = plan
        return plan

    def get_plan(self, plan_id: str, *, tenant_id: str) -> PublicationPlan:
        tenant = require_tenant_id(tenant_id)
        plan = self._plans.get(tenant, {}).get(plan_id)
        if plan is None:
            for other, items in self._plans.items():
                if other != tenant and plan_id in items:
                    raise GovernedPublishError(PUBLISH_ACCESS_DENIED)
            raise GovernedPublishError(PUBLISH_NOT_FOUND)
        return plan

    def save_receipt(self, receipt: PublicationReceipt) -> PublicationReceipt:
        tenant = require_tenant_id(receipt.tenant_id)
        self._receipts.setdefault(tenant, {})[receipt.receipt_id] = receipt
        return receipt

    def get_receipt(self, receipt_id: str, *, tenant_id: str) -> PublicationReceipt:
        tenant = require_tenant_id(tenant_id)
        rec = self._receipts.get(tenant, {}).get(receipt_id)
        if rec is None:
            for other, items in self._receipts.items():
                if other != tenant and receipt_id in items:
                    raise GovernedPublishError(PUBLISH_ACCESS_DENIED)
            raise GovernedPublishError(PUBLISH_NOT_FOUND)
        return rec

    def receipt_for_key(self, *, tenant_id: str, key: str) -> PublicationReceipt | None:
        tenant = require_tenant_id(tenant_id)
        for rec in self._receipts.get(tenant, {}).values():
            if rec.idempotency_key == key:
                return rec
        return None

    def audit(self, tenant_id: str, event: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        self._audit.setdefault(tenant, []).append(dict(event))

    def list_audit(self, *, tenant_id: str) -> list[dict]:
        return list(self._audit.get(require_tenant_id(tenant_id), []))

    def remember_execution(self, *, key: str, receipt_id: str) -> None:
        now = utc_now()
        self._idem.put(
            IdempotencyRecord(
                key=key,
                action_id=key,
                state=IDEMPOTENCY_COMPLETED,
                created_at=now,
                updated_at=now,
                execution_id=receipt_id,
            )
        )

    def completed(self, key: str) -> IdempotencyRecord | None:
        rec = self._idem.get(key)
        if rec and rec.state == IDEMPOTENCY_COMPLETED:
            return rec
        return None
