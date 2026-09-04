"""Block 18 — minimum-price / margin protection. Reuses data_intel.economics only."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from data_intel.economics import (
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_REVIEW,
    DECISION_WARN,
    DISCOUNT_PLATFORM,
    DISCOUNT_SELLER,
    DISCOUNT_UNKNOWN,
    EconomicsInput,
    EconomicsPolicy,
    calculate_economics,
    calculate_minimum_price,
)

from commerce_ops.errors import (
    INCOMPLETE_ECONOMICS,
    INVALID_PRICE,
    UNKNOWN_DISCOUNT_OWNERSHIP,
    UNKNOWN_PRICE,
    UNSAFE_PRICE,
)

OWNER_SELLER = "SELLER_FUNDED"
OWNER_MARKETPLACE = "MARKETPLACE_FUNDED"
OWNER_SHARED = "SHARED"
OWNER_UNKNOWN = "UNKNOWN"

_OWNER_MAP = {
    OWNER_SELLER: DISCOUNT_SELLER,
    "SELLER": DISCOUNT_SELLER,
    OWNER_MARKETPLACE: DISCOUNT_PLATFORM,
    "PLATFORM": DISCOUNT_PLATFORM,
    "MARKETPLACE_FUNDED": DISCOUNT_PLATFORM,
    OWNER_SHARED: DISCOUNT_UNKNOWN,  # fail-closed unless parts explicit
    OWNER_UNKNOWN: DISCOUNT_UNKNOWN,
    DISCOUNT_UNKNOWN: DISCOUNT_UNKNOWN,
}


def map_discount_ownership(raw: str | None) -> str:
    key = str(raw or OWNER_UNKNOWN).strip().upper()
    return _OWNER_MAP.get(key, DISCOUNT_UNKNOWN)


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def evaluate_proposed_price(
    base: EconomicsInput,
    *,
    proposed: Decimal | None,
    policy: EconomicsPolicy | None = None,
    require_costs: tuple[str, ...] = (),
) -> dict:
    """Authoritative safety gate. UNKNOWN costs are not treated as 0."""
    policy = policy or EconomicsPolicy()
    if proposed is None:
        return {
            "decision": DECISION_REQUIRE_REVIEW,
            "code": UNKNOWN_PRICE,
            "minimum_price": None,
            "economics": None,
            "mutate": False,
        }
    try:
        proposed_d = Decimal(str(proposed))
    except Exception:
        return {
            "decision": DECISION_DENY,
            "code": INVALID_PRICE,
            "minimum_price": None,
            "economics": None,
            "mutate": False,
        }
    if proposed_d < 0:
        return {
            "decision": DECISION_DENY,
            "code": INVALID_PRICE,
            "minimum_price": None,
            "economics": None,
            "mutate": False,
        }

    inp = replace(base, selling_price=proposed_d, selling_price_prov=base.selling_price_prov or "USER_PROVIDED")
    if inp.discount_ownership == DISCOUNT_UNKNOWN and (
        inp.discount_rate or inp.discount_amount or inp.marketplace_subsidy or inp.seller_subsidy
    ):
        return {
            "decision": DECISION_DENY,
            "code": UNKNOWN_DISCOUNT_OWNERSHIP,
            "minimum_price": None,
            "economics": None,
            "mutate": False,
            "effective_seller_revenue": None,
            "marketplace_subsidy": str(inp.marketplace_subsidy) if inp.marketplace_subsidy is not None else None,
            "seller_funded_discount": str(inp.discount_amount or inp.discount_rate or ""),
        }

    econ = calculate_economics(inp, policy=policy)
    floor, floor_label, floor_unknown = calculate_minimum_price(inp, policy=policy)
    missing = list(econ.get("missing") or []) + list(floor_unknown)
    for req in require_costs:
        if req == "advertising" and inp.advertising_cost is None and inp.advertising_rate is None:
            missing.append("advertising")
        if req == "logistics" and inp.logistics_cost is None:
            missing.append("logistics")

    decision = str(econ.get("decision") or DECISION_REQUIRE_REVIEW)
    code = None
    if missing and floor is None:
        decision = DECISION_REQUIRE_REVIEW
        code = INCOMPLETE_ECONOMICS
    if proposed_d is not None and floor is not None and proposed_d < floor:
        decision = DECISION_DENY
        code = UNSAFE_PRICE
    if decision == DECISION_DENY and code is None:
        code = UNSAFE_PRICE

    return {
        "decision": decision,
        "code": code,
        "minimum_price": str(floor) if floor is not None else None,
        "floor_label": floor_label,
        "economics": econ,
        "missing": tuple(dict.fromkeys(missing)),
        "mutate": decision in {DECISION_ALLOW, DECISION_WARN},
        "contribution_note": econ.get("note"),
        "effective_price": econ.get("effective_price"),
        "marketplace_subsidy": str(inp.marketplace_subsidy) if inp.marketplace_subsidy is not None else None,
        "discount_note": econ.get("discount_note"),
        "completeness": econ.get("completeness"),
    }
