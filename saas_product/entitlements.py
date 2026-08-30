"""Effective entitlement resolution."""

from __future__ import annotations

from datetime import datetime, timezone

from saas_product.models import SUB_ACTIVE, SUB_FREE, SUB_TRIALING, EffectiveEntitlements, SubscriptionRecord
from saas_product.plans import PLAN_FREE, get_plan


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class EntitlementService:
    def resolve(self, *, tenant_id: str, subscription: SubscriptionRecord | None) -> EffectiveEntitlements:
        if subscription is None or subscription.status not in {SUB_ACTIVE, SUB_TRIALING}:
            free = get_plan(PLAN_FREE, "2026-01")
            features = frozenset(free.entitlements.get("features", [])) if free else frozenset({"chat"})
            quotas = dict(free.quotas) if free else {"requests_per_month": 100}
            return EffectiveEntitlements(
                tenant_id=tenant_id,
                plan_id=PLAN_FREE,
                plan_version="2026-01",
                subscription_status="FREE",
                features=features,
                quotas=quotas,
                generated_at=_utc(),
            )
        plan = get_plan(subscription.plan_id, subscription.plan_version)
        if plan is None:
            return EffectiveEntitlements(
                tenant_id=tenant_id,
                plan_id=subscription.plan_id,
                plan_version=subscription.plan_version,
                subscription_status=subscription.status,
                features=frozenset(),
                quotas={},
                generated_at=_utc(),
            )
        features = frozenset(plan.entitlements.get("features", []))
        return EffectiveEntitlements(
            tenant_id=tenant_id,
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            subscription_status=subscription.status,
            features=features,
            quotas=dict(plan.quotas),
            generated_at=_utc(),
        )

    def check_quota(self, entitlements: EffectiveEntitlements, meter: str, used: int, delta: int = 1) -> bool:
        limit = entitlements.quota_limit(meter)
        if limit is None:
            return True
        return used + delta <= limit
