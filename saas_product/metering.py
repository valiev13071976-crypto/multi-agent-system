"""Commercial usage metering projection over canonical usage."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from threading import Lock

from saas_product.models import UsageMeterRecord


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def period_key_month(when: datetime | None = None) -> str:
    stamp = when or datetime.now(timezone.utc)
    return f"{stamp.year:04d}-{stamp.month:02d}"


class MeteringService:
    METER_REQUESTS = "requests_per_month"

    def __init__(self, *, store, finops=None):
        self.store = store
        self.finops = finops
        self._reserve_lock = Lock()

    def record_request(self, *, tenant_id: str, user_id: str, idempotency_key: str, quantity: int = 1) -> UsageMeterRecord:
        key = period_key_month()
        rec = UsageMeterRecord(
            meter_id=self.METER_REQUESTS,
            tenant_id=tenant_id,
            user_id=user_id,
            quantity=quantity,
            period_key=key,
            idempotency_key=idempotency_key,
            created_at=_utc(),
        )
        return self.store.record_usage_meter(rec)

    def usage_for_tenant(self, tenant_id: str, *, meter_id: str | None = None) -> int:
        meter = meter_id or self.METER_REQUESTS
        return self.store.sum_usage_meter(tenant_id, meter, period_key_month())

    def try_reserve_request(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        idempotency_key: str,
        quantity: int = 1,
    ) -> bool:
        with self._reserve_lock:
            return self.store.try_reserve_usage(
                tenant_id=tenant_id,
                meter_id=self.METER_REQUESTS,
                period_key=period_key_month(),
                limit=limit,
                idempotency_key=idempotency_key,
                user_id=user_id,
                quantity=quantity,
            )
