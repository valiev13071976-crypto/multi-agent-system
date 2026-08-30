"""Deterministic pricing engine (Block 11.4 / 11.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from commerce.product_platform.models import (
    PRICE_ALLOW,
    PRICE_DENY,
    PRICE_INSUFFICIENT_DATA,
    PRICE_NO_CHANGE,
    PRICE_REQUIRE_APPROVAL,
    MoneyAmount,
    PriceDecision,
    PricePolicy,
)


def round_price(amount: Decimal, *, scale: int = 2) -> Decimal:
    quant = Decimal("1").scaleb(-scale)
    return amount.quantize(quant, rounding=ROUND_HALF_UP)


def evaluate_price_decision(
    *,
    decision_id: str,
    tenant_id: str,
    product_id: str,
    policy: PricePolicy,
    current: MoneyAmount,
    proposed: MoneyAmount,
    trusted_cost: MoneyAmount | None,
    observations_fresh: bool,
    outlier: bool = False,
    price_version: int = 1,
) -> PriceDecision:
    reasons: list[str] = []
    if current.currency != proposed.currency or current.currency != policy.currency:
        return PriceDecision(
            decision_id=decision_id,
            tenant_id=tenant_id,
            product_id=product_id,
            policy_version=policy.version,
            current_price=current,
            proposed_price=proposed,
            trusted_cost=trusted_cost,
            outcome=PRICE_DENY,
            reasons=("currency_mismatch",),
            price_version=price_version,
        )
    if not observations_fresh:
        return PriceDecision(
            decision_id=decision_id,
            tenant_id=tenant_id,
            product_id=product_id,
            policy_version=policy.version,
            current_price=current,
            proposed_price=proposed,
            trusted_cost=trusted_cost,
            outcome=PRICE_INSUFFICIENT_DATA,
            reasons=("stale_observations",),
            price_version=price_version,
        )
    if outlier:
        return PriceDecision(
            decision_id=decision_id,
            tenant_id=tenant_id,
            product_id=product_id,
            policy_version=policy.version,
            current_price=current,
            proposed_price=proposed,
            trusted_cost=trusted_cost,
            outcome=PRICE_REQUIRE_APPROVAL,
            reasons=("outlier_observation",),
            price_version=price_version,
        )
    proposed_amount = round_price(proposed.amount, scale=policy.rounding_scale)
    if proposed_amount == current.amount:
        return PriceDecision(
            decision_id=decision_id,
            tenant_id=tenant_id,
            product_id=product_id,
            policy_version=policy.version,
            current_price=current,
            proposed_price=MoneyAmount(proposed_amount, proposed.currency),
            trusted_cost=trusted_cost,
            outcome=PRICE_NO_CHANGE,
            reasons=tuple(reasons),
            price_version=price_version,
        )
    if proposed_amount < policy.minimum_price:
        reasons.append("below_minimum_price")
        return PriceDecision(
            decision_id=decision_id,
            tenant_id=tenant_id,
            product_id=product_id,
            policy_version=policy.version,
            current_price=current,
            proposed_price=MoneyAmount(proposed_amount, proposed.currency),
            trusted_cost=trusted_cost,
            outcome=PRICE_DENY,
            reasons=tuple(reasons),
            price_version=price_version,
        )
    if proposed_amount > policy.maximum_price:
        reasons.append("above_maximum_price")
        return PriceDecision(
            decision_id=decision_id,
            tenant_id=tenant_id,
            product_id=product_id,
            policy_version=policy.version,
            current_price=current,
            proposed_price=MoneyAmount(proposed_amount, proposed.currency),
            trusted_cost=trusted_cost,
            outcome=PRICE_DENY,
            reasons=tuple(reasons),
            price_version=price_version,
        )
    if trusted_cost is not None:
        margin = (proposed_amount - trusted_cost.amount) / trusted_cost.amount * Decimal("100")
        if margin < policy.minimum_margin_pct:
            reasons.append("below_minimum_margin")
            return PriceDecision(
                decision_id=decision_id,
                tenant_id=tenant_id,
                product_id=product_id,
                policy_version=policy.version,
                current_price=current,
                proposed_price=MoneyAmount(proposed_amount, proposed.currency),
                trusted_cost=trusted_cost,
                outcome=PRICE_DENY,
                reasons=tuple(reasons),
                price_version=price_version,
            )
    delta = proposed_amount - current.amount
    if abs(delta) > policy.max_change_abs:
        reasons.append("absolute_change_exceeded")
        outcome = PRICE_REQUIRE_APPROVAL if abs(delta) <= policy.max_change_abs * 2 else PRICE_DENY
        return PriceDecision(
            decision_id=decision_id,
            tenant_id=tenant_id,
            product_id=product_id,
            policy_version=policy.version,
            current_price=current,
            proposed_price=MoneyAmount(proposed_amount, proposed.currency),
            trusted_cost=trusted_cost,
            outcome=outcome,
            reasons=tuple(reasons),
            price_version=price_version,
        )
    if current.amount > 0:
        pct = abs(delta / current.amount * Decimal("100"))
        if pct > policy.max_change_pct:
            reasons.append("percent_change_exceeded")
            return PriceDecision(
                decision_id=decision_id,
                tenant_id=tenant_id,
                product_id=product_id,
                policy_version=policy.version,
                current_price=current,
                proposed_price=MoneyAmount(proposed_amount, proposed.currency),
                trusted_cost=trusted_cost,
                outcome=PRICE_REQUIRE_APPROVAL,
                reasons=tuple(reasons),
                price_version=price_version,
            )
        if pct > policy.auto_apply_max_change_pct:
            reasons.append("approval_band")
            return PriceDecision(
                decision_id=decision_id,
                tenant_id=tenant_id,
                product_id=product_id,
                policy_version=policy.version,
                current_price=current,
                proposed_price=MoneyAmount(proposed_amount, proposed.currency),
                trusted_cost=trusted_cost,
                outcome=PRICE_REQUIRE_APPROVAL,
                reasons=tuple(reasons),
                price_version=price_version,
            )
    return PriceDecision(
        decision_id=decision_id,
        tenant_id=tenant_id,
        product_id=product_id,
        policy_version=policy.version,
        current_price=current,
        proposed_price=MoneyAmount(proposed_amount, proposed.currency),
        trusted_cost=trusted_cost,
        outcome=PRICE_ALLOW,
        reasons=tuple(reasons),
        price_version=price_version,
    )


def observation_is_fresh(observed_at: datetime, *, max_age_sec: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return (now - observed_at).total_seconds() <= max_age_sec


def is_outlier(previous: Decimal | None, observed: Decimal, *, threshold_pct: Decimal = Decimal("80")) -> bool:
    if previous is None or previous <= 0:
        return False
    drop_pct = (previous - observed) / previous * Decimal("100")
    return drop_pct >= threshold_pct
