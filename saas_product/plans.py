"""Product plan catalog — versioned authoritative plans."""

from __future__ import annotations

from saas_product.models import PlanRecord

PLAN_FREE = "free"
PLAN_STARTER = "starter"
PLAN_PRO = "pro"

PLAN_CATALOG: dict[tuple[str, str], PlanRecord] = {}


def _register(plan: PlanRecord) -> None:
    PLAN_CATALOG[(plan.plan_id, plan.plan_version)] = plan


_register(
    PlanRecord(
        plan_id=PLAN_FREE,
        plan_version="2026-01",
        name="Free",
        status="ACTIVE",
        billing_interval="month",
        currency="USD",
        price_minor=0,
        entitlements={"features": ["chat", "basic_tools"]},
        quotas={"requests_per_month": 100, "members": 3},
    )
)
_register(
    PlanRecord(
        plan_id=PLAN_STARTER,
        plan_version="2026-01",
        name="Starter",
        status="ACTIVE",
        billing_interval="month",
        currency="USD",
        price_minor=2900,
        entitlements={"features": ["chat", "basic_tools", "workflows"]},
        quotas={"requests_per_month": 5000, "members": 10},
    )
)
_register(
    PlanRecord(
        plan_id=PLAN_PRO,
        plan_version="2026-01",
        name="Pro",
        status="ACTIVE",
        billing_interval="month",
        currency="USD",
        price_minor=9900,
        entitlements={"features": ["all"]},
        quotas={"requests_per_month": 50000, "members": 50},
    )
)


def get_plan(plan_id: str, plan_version: str) -> PlanRecord | None:
    return PLAN_CATALOG.get((plan_id, plan_version))


def list_plans() -> tuple[PlanRecord, ...]:
    return tuple(PLAN_CATALOG.values())
