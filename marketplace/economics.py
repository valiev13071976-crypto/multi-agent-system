"""Deterministic marketplace channel economics (Decimal only)."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from marketplace.models import (
    MIN_PRICE_POLICY_VERSION,
    PROFIT_BELOW_TARGET,
    PROFIT_LOSS,
    PROFIT_PROFITABLE,
    PROFIT_UNKNOWN,
    PROMO_LOSS,
    PROMO_PLATFORM,
    PROMO_RISK_UNKNOWN,
    PROMO_SAFE,
    PROMO_SELLER,
    PROMO_WARNING,
    MarketplaceCommissionObservation,
    MarketplaceMinPricePolicy,
    MarketplaceProfitabilityResult,
    MarketplacePromotionObservation,
    MoneyAmount,
)


def _q(amount: Decimal) -> Decimal:
    return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_minimum_allowed_price(
    *,
    purchase_cost: Decimal | None,
    commission: MarketplaceCommissionObservation | None,
    logistics: Decimal | None,
    acquiring_rate: Decimal | None,
    other_costs: Decimal = Decimal("0"),
    policy: MarketplaceMinPricePolicy,
    vat_included: bool = True,
) -> tuple[MoneyAmount | None, str, tuple[str, ...]]:
    """
    min_allowed = (cost + logistics + fixed_fee + other) / (1 - commission_rate - acquiring - margin)
    when all required inputs present; otherwise INSUFFICIENT_DATA.
    """
    unknown: list[str] = []
    if purchase_cost is None:
        unknown.append("purchase_cost")
    if policy.include_commission and commission is None:
        unknown.append("commission")
    if policy.include_logistics and logistics is None:
        unknown.append("logistics")
    if policy.include_acquiring and acquiring_rate is None:
        unknown.append("acquiring")
    if unknown:
        return None, "INSUFFICIENT_DATA", tuple(unknown)

    cost = Decimal(str(purchase_cost))
    logi = Decimal(str(logistics or 0))
    fixed = Decimal(str(commission.fixed_fee if commission else 0))
    other = Decimal(str(other_costs))
    rate = Decimal(str(commission.rate if commission else 0))
    acq = Decimal(str(acquiring_rate or 0))
    margin = Decimal(str(policy.required_margin_pct)) / Decimal("100")
    denom = Decimal("1") - rate - acq - margin
    if denom <= 0:
        return None, "INSUFFICIENT_DATA", ("invalid_rate_stack",)
    base = cost + logi + fixed + other
    # VAT context: amounts already treated as configured; do not invent VAT from price alone.
    _ = vat_included
    min_price = _q(base / denom)
    evidence = (
        f"cost={cost}",
        f"logistics={logi}",
        f"commission_rate={rate}",
        f"fixed_fee={fixed}",
        f"acquiring={acq}",
        f"margin={margin}",
        f"policy={policy.version}",
    )
    return MoneyAmount(min_price, policy.currency), "OK", evidence


def calculate_profitability(
    *,
    sku_id: str,
    provider: str,
    selling_price: Decimal,
    purchase_cost: Decimal | None,
    commission: MarketplaceCommissionObservation | None,
    logistics: Decimal | None,
    acquiring_rate: Decimal | None = None,
    other_costs: Decimal = Decimal("0"),
    policy: MarketplaceMinPricePolicy | None = None,
    currency: str = "RUB",
) -> MarketplaceProfitabilityResult:
    policy = policy or MarketplaceMinPricePolicy(policy_id="default")
    unknown: list[str] = []
    if purchase_cost is None:
        unknown.append("purchase_cost")
    if commission is None:
        unknown.append("commission")
    if logistics is None:
        unknown.append("logistics")

    price = _q(Decimal(str(selling_price)))
    if unknown:
        return MarketplaceProfitabilityResult(
            sku_id=sku_id,
            provider=provider,
            selling_price=MoneyAmount(price, currency),
            estimated_proceeds=MoneyAmount(price, currency),
            known_costs=MoneyAmount(Decimal("0"), currency),
            unknown_costs=tuple(unknown),
            contribution=MoneyAmount(Decimal("0"), currency),
            margin_pct=None,
            minimum_allowed=None,
            status=PROFIT_UNKNOWN,
            evidence=("incomplete_cost_stack",),
            policy_version=policy.version,
        )

    rate = Decimal(str(commission.rate))
    fixed = Decimal(str(commission.fixed_fee))
    logi = Decimal(str(logistics))
    acq = Decimal(str(acquiring_rate or 0)) * price
    commission_amt = _q(price * rate) + fixed
    proceeds = _q(price - commission_amt - logi - acq)
    known = _q(Decimal(str(purchase_cost)) + commission_amt + logi + acq + Decimal(str(other_costs)))
    contribution = _q(price - known)
    margin = _q((contribution / price) * Decimal("100")) if price else None
    min_allowed, _, evidence = calculate_minimum_allowed_price(
        purchase_cost=purchase_cost,
        commission=commission,
        logistics=logistics,
        acquiring_rate=acquiring_rate,
        other_costs=other_costs,
        policy=policy,
    )
    target = Decimal(str(policy.required_margin_pct))
    if contribution < 0:
        status = PROFIT_LOSS
    elif margin is not None and margin < target:
        status = PROFIT_BELOW_TARGET
    else:
        status = PROFIT_PROFITABLE
    if min_allowed is not None and price < min_allowed.amount:
        status = PROFIT_LOSS
    return MarketplaceProfitabilityResult(
        sku_id=sku_id,
        provider=provider,
        selling_price=MoneyAmount(price, currency),
        estimated_proceeds=MoneyAmount(proceeds, currency),
        known_costs=MoneyAmount(known, currency),
        unknown_costs=(),
        contribution=MoneyAmount(contribution, currency),
        margin_pct=margin,
        minimum_allowed=min_allowed,
        status=status,
        evidence=evidence + (f"contribution={contribution}", f"status={status}"),
        policy_version=policy.version or MIN_PRICE_POLICY_VERSION,
    )


def assess_promotion_risk(
    *,
    promo: MarketplacePromotionObservation,
    profitability: MarketplaceProfitabilityResult,
) -> dict:
    """Platform-funded discounts are not assumed seller losses."""
    if promo.ownership == PROMO_PLATFORM:
        return {
            "risk": PROMO_SAFE,
            "reason": "platform_funded_discount_not_seller_loss",
            "ownership": PROMO_PLATFORM,
            "use_price": str(promo.seller_price.amount if promo.seller_price else promo.displayed_price.amount),
        }
    if profitability.status == PROFIT_UNKNOWN:
        return {"risk": PROMO_RISK_UNKNOWN, "reason": "unknown_economics", "ownership": promo.ownership}
    if profitability.status == PROFIT_LOSS:
        return {"risk": PROMO_LOSS, "reason": "seller_economics_below_floor", "ownership": promo.ownership}
    if profitability.status == PROFIT_BELOW_TARGET:
        return {"risk": PROMO_WARNING, "reason": "below_margin_target", "ownership": promo.ownership}
    if promo.ownership == PROMO_SELLER and profitability.minimum_allowed:
        effective = promo.displayed_price.amount
        if effective < profitability.minimum_allowed.amount:
            return {"risk": PROMO_LOSS, "reason": "seller_discount_below_floor", "ownership": PROMO_SELLER}
    return {"risk": PROMO_SAFE, "reason": "within_policy", "ownership": promo.ownership}


def scenario_price(
    *,
    sku_id: str,
    provider: str,
    price: Decimal,
    purchase_cost: Decimal | None,
    commission: MarketplaceCommissionObservation | None,
    logistics: Decimal | None,
    policy: MarketplaceMinPricePolicy,
) -> MarketplaceProfitabilityResult:
    return calculate_profitability(
        sku_id=sku_id,
        provider=provider,
        selling_price=price,
        purchase_cost=purchase_cost,
        commission=commission,
        logistics=logistics,
        policy=policy,
    )
