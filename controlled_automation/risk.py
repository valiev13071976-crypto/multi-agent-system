"""Risk classification for controlled automations."""

from __future__ import annotations

R0_READ_ONLY = "R0_READ_ONLY"
R1_PREPARE_ONLY = "R1_PREPARE_ONLY"
R2_REVERSIBLE_LOW_RISK_WRITE = "R2_REVERSIBLE_LOW_RISK_WRITE"
R3_EXTERNAL_BUSINESS_WRITE = "R3_EXTERNAL_BUSINESS_WRITE"
R4_HIGH_IMPACT = "R4_HIGH_IMPACT"

RISK_CLASSES = frozenset({R0_READ_ONLY, R1_PREPARE_ONLY, R2_REVERSIBLE_LOW_RISK_WRITE, R3_EXTERNAL_BUSINESS_WRITE, R4_HIGH_IMPACT})

READ_ACTIONS = frozenset({"ANALYTICS_READ", "STOCK_READ", "PRICE_READ", "SEO_ANALYZE"})
PREPARE_ACTIONS = frozenset({
    "PREPARE_PRICE_UPDATE",
    "PREPARE_PRODUCT_UPDATE",
    "PREPARE_MARKETPLACE_CARD",
    "PREPARE_EMAIL",
    "PREPARE_CALENDAR_EVENT",
    "PREPARE_CRM_UPDATE",
    "PREPARE_BITRIX_UPDATE",
    "CONTENT_GENERATE",
})
WRITE_ACTIONS = frozenset({
    "MARKETPLACE_PRICE_UPDATE",
    "BITRIX_PRODUCT_UPDATE",
    "CRM_UPDATE",
    "CALENDAR_CREATE",
    "EMAIL_SEND",
})


def default_risk_for_action(action_type: str) -> str:
    if action_type in READ_ACTIONS:
        return R0_READ_ONLY
    if action_type in PREPARE_ACTIONS:
        return R1_PREPARE_ONLY
    if action_type in WRITE_ACTIONS:
        return R3_EXTERNAL_BUSINESS_WRITE
    return R4_HIGH_IMPACT


def requires_hitl(*, risk_class: str, allow_auto_execute: bool) -> bool:
    if risk_class in {R3_EXTERNAL_BUSINESS_WRITE, R4_HIGH_IMPACT}:
        return True
    if risk_class == R2_REVERSIBLE_LOW_RISK_WRITE and not allow_auto_execute:
        return True
    return False


def can_auto_execute(*, risk_class: str, allow_auto_execute: bool, dry_run: bool) -> bool:
    if dry_run:
        return True
    if risk_class == R0_READ_ONLY:
        return True
    if risk_class == R1_PREPARE_ONLY:
        return True
    if risk_class == R2_REVERSIBLE_LOW_RISK_WRITE:
        return allow_auto_execute
    return False
