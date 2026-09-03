"""Canonical product plans — entitlements and limits (not final commercial pricing)."""

from __future__ import annotations

from accounts.models import (
    ALL_ENTITLEMENTS,
    ENT_ADVANCED_ANALYTICS,
    ENT_BUSINESS_ASSISTANT,
    ENT_CHAT_ACCESS,
    ENT_EXCEL_ANALYSIS,
    ENT_FILE_UPLOAD,
    ENT_IMAGE_GENERATION,
    ENT_MARKETPLACE_TOOLS,
    ENT_SCHEDULED_AUTOMATION,
    ENT_TELEGRAM,
    ENT_VOICE,
    ENT_WEB_SEARCH,
    PlanDefinition,
)

PLAN_TRIAL = "trial"
PLAN_BASIC = "basic"
PLAN_PRO = "pro"

# Map legacy saas_product plan ids
LEGACY_PLAN_MAP = {
    "free": PLAN_TRIAL,
    "starter": PLAN_BASIC,
    "pro": PLAN_PRO,
}


_PLANS: dict[str, PlanDefinition] = {
    PLAN_TRIAL: PlanDefinition(
        plan_id=PLAN_TRIAL,
        code="TRIAL",
        name="Trial",
        status="ACTIVE",
        display_order=10,
        entitlements=frozenset({ENT_CHAT_ACCESS, ENT_FILE_UPLOAD, ENT_BUSINESS_ASSISTANT}),
        limits={"requests_per_day": 50, "requests_per_month": 500, "ai_cost_budget_minor": 500},
    ),
    PLAN_BASIC: PlanDefinition(
        plan_id=PLAN_BASIC,
        code="BASIC",
        name="Basic",
        status="ACTIVE",
        display_order=20,
        entitlements=frozenset(
            {
                ENT_CHAT_ACCESS,
                ENT_WEB_SEARCH,
                ENT_FILE_UPLOAD,
                ENT_EXCEL_ANALYSIS,
                ENT_BUSINESS_ASSISTANT,
            }
        ),
        limits={"requests_per_day": 500, "requests_per_month": 5000, "ai_cost_budget_minor": 5000},
    ),
    PLAN_PRO: PlanDefinition(
        plan_id=PLAN_PRO,
        code="PRO",
        name="Pro",
        status="ACTIVE",
        display_order=30,
        entitlements=frozenset(ALL_ENTITLEMENTS),
        limits={
            "requests_per_day": 5000,
            "requests_per_month": 50000,
            "ai_cost_budget_minor": 50000,
            "image_generation_count": 200,
            "automation_count": 100,
        },
    ),
}


def get_plan(plan_id: str) -> PlanDefinition | None:
    pid = LEGACY_PLAN_MAP.get(plan_id, plan_id)
    return _PLANS.get(pid)


def list_plans() -> tuple[PlanDefinition, ...]:
    return tuple(sorted(_PLANS.values(), key=lambda p: p.display_order))


def normalize_plan_id(plan_id: str) -> str:
    return LEGACY_PLAN_MAP.get(plan_id, plan_id)


def plan_safe_view(plan: PlanDefinition) -> dict:
    return {
        "plan_id": plan.plan_id,
        "code": plan.code,
        "name": plan.name,
        "status": plan.status,
        "entitlements": sorted(plan.entitlements),
        "limits": dict(plan.limits),
    }
