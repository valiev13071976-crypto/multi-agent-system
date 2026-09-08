"""Block 5.8 — canonical Marketplace Price Protection engine.

Architecture (spec section 1):

    Product economics (canonical, vendor-neutral ``PriceProtectionContext``)
        -> Price Protection calculation (this module)
            -> Decision / guardrails (ALLOW / REQUIRE_APPROVAL / BLOCK)
                -> existing governed marketplace price write (5.7A
                   ``MarketplacePlatform.write_price``)
                    -> WB / Ozon / Yandex Market adapter

This module is intentionally self-contained and additive:

- It does NOT replace or modify ``marketplace.economics`` /
  ``marketplace.price_guard`` (the existing engine that reacts to
  marketplace-*observed* price/promotion events for auto-correct/alerting
  -- a different problem: reacting to a price the marketplace already
  shows, not gating a price Panda is about to *write*). Their vocabulary
  (``MarketplaceCommissionObservation``/``MarketplaceMinPricePolicy``/
  ``PROFIT_*``) has its own internal percentage convention already
  exercised by real callers (``integrations.{wildberries,ozon,
  yandex_market}``); this module defines its own explicit, unambiguous
  convention instead of overloading that one (spec section 2).
- It does NOT implement a second HITL/approval engine. The existing
  ``approved_write`` governed-write flag (``IntegrationActivationService.
  execute_via_gateway`` / every ``MarketplacePlatform`` write method) is
  reused as-is. This module only decides *whether* a given write requires
  that flag to be true (``REQUIRE_APPROVAL``), or must never be allowed
  regardless of it (``BLOCK`` -- the hard economic floor), or needs no
  special scrutiny (``ALLOW``).
- It does NOT invent an FX subsystem. Currency mismatches fail closed
  (``BLOCK``/``CURRENCY_MISMATCH``).

Percentage convention (spec section 2, made explicit to remove all
ambiguity): every ``*_rate`` field on ``PriceProtectionContext`` /
``PriceProtectionPolicy`` is a **fraction of 1** (e.g. ``Decimal("0.15")``
means 15%), valid only in ``[0, 1)``. Never a "10 means 10%" bare integer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from security.tenant import require_tenant_id

from marketplace.models import MoneyAmount

# ---- decision outcomes (spec section 6) ----
PRICE_DECISION_ALLOW = "ALLOW"
PRICE_DECISION_REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
PRICE_DECISION_BLOCK = "BLOCK"
PRICE_DECISION_OUTCOMES = (PRICE_DECISION_ALLOW, PRICE_DECISION_REQUIRE_APPROVAL, PRICE_DECISION_BLOCK)

# ---- decision reason codes (spec section 6, minimum required set) ----
REASON_BELOW_MINIMUM_ALLOWED_PRICE = "BELOW_MINIMUM_ALLOWED_PRICE"
REASON_LOSS_MAKING = "LOSS_MAKING"
REASON_BELOW_MINIMUM_PROFIT = "BELOW_MINIMUM_PROFIT"
REASON_BELOW_MINIMUM_MARGIN = "BELOW_MINIMUM_MARGIN"
REASON_PRICE_DROP_LIMIT_EXCEEDED = "PRICE_DROP_LIMIT_EXCEEDED"
REASON_PRICE_INCREASE_LIMIT_EXCEEDED = "PRICE_INCREASE_LIMIT_EXCEEDED"
REASON_CRITICAL_PRICE_CHANGE = "CRITICAL_PRICE_CHANGE"
REASON_INVALID_ECONOMIC_INPUT = "INVALID_ECONOMIC_INPUT"
REASON_CURRENCY_MISMATCH = "CURRENCY_MISMATCH"

# ---- profitability classification vocabulary (spec section 5) ----
PROFITABILITY_SAFE = "SAFE"
PROFITABILITY_BELOW_TARGET_MARGIN = "BELOW_TARGET_MARGIN"
PROFITABILITY_BELOW_MINIMUM_PROFIT = "BELOW_MINIMUM_PROFIT"
PROFITABILITY_LOSS_MAKING = "LOSS_MAKING"
PROFITABILITY_INVALID_ECONOMICS = "INVALID_ECONOMICS"


def _q(amount: Decimal) -> Decimal:
    return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _dec(value) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class PriceProtectionContext:
    """Vendor-neutral economic input for one product/SKU/provider price
    decision (spec section 2). ``purchase_cost`` is the one field that
    must be explicitly supplied (``None`` is treated as *missing*, never
    silently defaulted to zero); every other cost component defaults to
    ``Decimal("0")`` -- a legitimate, explicitly-configured "no such cost"
    value, not a guess. ``minimum_profit_amount``/``minimum_margin_rate``
    default to ``None`` (not configured); when both are ``None`` the floor
    is simple break-even (never loss-making) -- see
    ``calculate_minimum_allowed_price``.
    """

    tenant_id: str
    product_id: str
    sku: str
    provider: str
    currency: str = "RUB"

    purchase_cost: Decimal | None = None
    additional_unit_cost: Decimal = Decimal("0")

    commission_rate: Decimal = Decimal("0")
    commission_fixed: Decimal = Decimal("0")
    logistics_cost: Decimal = Decimal("0")
    last_mile_cost: Decimal = Decimal("0")
    fulfillment_cost: Decimal = Decimal("0")
    storage_cost: Decimal = Decimal("0")
    return_allowance: Decimal = Decimal("0")
    acquiring_rate: Decimal = Decimal("0")
    acquiring_fixed: Decimal = Decimal("0")
    advertising_rate: Decimal = Decimal("0")
    advertising_fixed: Decimal = Decimal("0")
    packaging_cost: Decimal = Decimal("0")
    other_costs: Decimal = Decimal("0")

    tax_rate: Decimal = Decimal("0")
    tax_fixed: Decimal = Decimal("0")

    minimum_profit_amount: Decimal | None = None
    minimum_margin_rate: Decimal | None = None

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        if self.purchase_cost is not None:
            object.__setattr__(self, "purchase_cost", _dec(self.purchase_cost))
        for name in (
            "additional_unit_cost", "commission_rate", "commission_fixed", "logistics_cost",
            "last_mile_cost", "fulfillment_cost", "storage_cost", "return_allowance",
            "acquiring_rate", "acquiring_fixed", "advertising_rate", "advertising_fixed",
            "packaging_cost", "other_costs", "tax_rate", "tax_fixed",
        ):
            object.__setattr__(self, name, _dec(getattr(self, name)))
        if self.minimum_profit_amount is not None:
            object.__setattr__(self, "minimum_profit_amount", _dec(self.minimum_profit_amount))
        if self.minimum_margin_rate is not None:
            object.__setattr__(self, "minimum_margin_rate", _dec(self.minimum_margin_rate))


@dataclass(frozen=True)
class PriceProtectionPolicy:
    """Tenant/provider/SKU-scoped configurable limits (spec section 8).
    Percent fields use the same fraction-of-1 convention as
    ``PriceProtectionContext`` rates would, EXCEPT these are already
    expressed the way an operator configures a percentage limit -- e.g.
    ``Decimal("15")`` means "15%" -- because they are compared directly
    against a computed percentage delta (see ``evaluate_price_decision``),
    not multiplied into a price. This is the one deliberate, documented
    exception to the fraction-of-1 rule, kept distinct from the *rate*
    fields on ``PriceProtectionContext`` (which multiply directly into a
    price and therefore MUST be fractions of 1).
    """

    policy_id: str = "default"
    maximum_price_drop_percent: Decimal | None = None
    maximum_price_increase_percent: Decimal | None = None
    critical_change_percent: Decimal | None = None
    require_approval_for_price_decrease: bool = False
    require_approval_for_critical_change: bool = True
    hard_floor_enabled: bool = True

    def __post_init__(self):
        for name in ("maximum_price_drop_percent", "maximum_price_increase_percent", "critical_change_percent"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _dec(value))


class PriceProtectionPolicyStore:
    """Tenant/provider/SKU-scoped policy store with deterministic
    precedence (spec section 8): SKU override -> provider policy -> tenant
    default -> a conservative system default (hard floor enabled, no
    policy-level friction) when nothing has been configured at all. Not a
    generic rules DSL -- three explicit, bounded precedence tiers."""

    def __init__(self):
        self._sku: dict[tuple[str, str, str], PriceProtectionPolicy] = {}
        self._provider: dict[tuple[str, str], PriceProtectionPolicy] = {}
        self._tenant: dict[str, PriceProtectionPolicy] = {}

    def set_tenant_default(self, *, tenant_id: str, policy: PriceProtectionPolicy) -> None:
        self._tenant[require_tenant_id(tenant_id)] = policy

    def set_provider_policy(self, *, tenant_id: str, provider: str, policy: PriceProtectionPolicy) -> None:
        self._provider[(require_tenant_id(tenant_id), provider)] = policy

    def set_sku_override(self, *, tenant_id: str, provider: str, sku: str, policy: PriceProtectionPolicy) -> None:
        self._sku[(require_tenant_id(tenant_id), provider, sku)] = policy

    def resolve(self, *, tenant_id: str, provider: str, sku: str) -> PriceProtectionPolicy:
        tenant = require_tenant_id(tenant_id)
        if (tenant, provider, sku) in self._sku:
            return self._sku[(tenant, provider, sku)]
        if (tenant, provider) in self._provider:
            return self._provider[(tenant, provider)]
        if tenant in self._tenant:
            return self._tenant[tenant]
        return PriceProtectionPolicy(policy_id="system_default")


@dataclass(frozen=True)
class PriceProtectionBreakdown:
    """Structured, explainable cost breakdown at one candidate price (spec
    section 3). Never just a boolean."""

    at_price: MoneyAmount
    purchase_cost: Decimal
    additional_unit_cost: Decimal
    marketplace_commission: Decimal
    logistics: Decimal
    last_mile: Decimal
    fulfillment: Decimal
    storage: Decimal
    return_allowance: Decimal
    acquiring: Decimal
    advertising: Decimal
    packaging: Decimal
    tax: Decimal
    other_costs: Decimal
    required_profit: Decimal
    total_cost_before_profit: Decimal
    minimum_profit_required: Decimal
    minimum_allowed_price: MoneyAmount | None
    expected_profit: Decimal
    expected_margin: Decimal | None  # percentage points, e.g. Decimal("15.00") == 15%


def _validate_context(context: PriceProtectionContext) -> tuple[str, ...]:
    """Structured invalid-input detection (spec section 13). Missing
    ``purchase_cost`` is never silently treated as zero."""
    problems: list[str] = []
    if context.purchase_cost is None:
        problems.append("purchase_cost_missing")
    elif context.purchase_cost < 0:
        problems.append("purchase_cost_negative")
    for name in (
        "additional_unit_cost", "commission_fixed", "logistics_cost", "last_mile_cost",
        "fulfillment_cost", "storage_cost", "return_allowance", "acquiring_fixed",
        "advertising_fixed", "packaging_cost", "other_costs", "tax_fixed",
    ):
        if getattr(context, name) < 0:
            problems.append(f"{name}_negative")
    for name in ("commission_rate", "acquiring_rate", "advertising_rate", "tax_rate"):
        value = getattr(context, name)
        if value < 0 or value >= 1:
            problems.append(f"{name}_out_of_domain")
    if context.minimum_margin_rate is not None and (context.minimum_margin_rate < 0 or context.minimum_margin_rate >= 1):
        problems.append("minimum_margin_rate_out_of_domain")
    if context.minimum_profit_amount is not None and context.minimum_profit_amount < 0:
        problems.append("minimum_profit_amount_negative")
    return tuple(problems)


def _fixed_costs(context: PriceProtectionContext) -> Decimal:
    return (
        Decimal(str(context.purchase_cost or 0))
        + context.additional_unit_cost
        + context.commission_fixed
        + context.logistics_cost
        + context.last_mile_cost
        + context.fulfillment_cost
        + context.storage_cost
        + context.return_allowance
        + context.acquiring_fixed
        + context.advertising_fixed
        + context.packaging_cost
        + context.other_costs
        + context.tax_fixed
    )


def _proportional_rate(context: PriceProtectionContext) -> Decimal:
    return context.commission_rate + context.acquiring_rate + context.advertising_rate + context.tax_rate


def calculate_minimum_allowed_price(context: PriceProtectionContext) -> tuple[MoneyAmount | None, str, tuple[str, ...]]:
    """Deterministically derive MINIMUM_ALLOWED_PRICE (spec section 4).

    Mathematically solves (never approximates) for the selling price P
    such that both fixed and *proportional-to-P* costs are covered plus
    the required profit:

        P = (fixed_costs + minimum_profit_amount) / (1 - proportional_rate)
        P = fixed_costs / (1 - proportional_rate - minimum_margin_rate)

    where ``proportional_rate = commission_rate + acquiring_rate +
    advertising_rate + tax_rate``. When both an absolute profit floor and
    a margin floor are configured, both candidate prices are computed and
    the larger (stricter) one wins. When neither is configured, the floor
    is simple break-even (required profit = 0) -- loss-making prices are
    never permitted even without an explicit profit policy.

    Returns ``(minimum_allowed_price, "OK", ())`` on success, or
    ``(None, "INVALID_ECONOMIC_INPUT", problem_codes)`` when the input is
    missing/invalid or the proportional cost stack makes any profitable
    price mathematically impossible.
    """
    problems = _validate_context(context)
    if problems:
        return None, "INVALID_ECONOMIC_INPUT", problems

    fixed = _fixed_costs(context)
    rate = _proportional_rate(context)
    if rate >= 1:
        return None, "INVALID_ECONOMIC_INPUT", ("proportional_costs_exceed_price",)

    candidates: list[Decimal] = []
    if context.minimum_profit_amount is not None:
        denom = Decimal("1") - rate
        candidates.append(_q((fixed + context.minimum_profit_amount) / denom))
    if context.minimum_margin_rate is not None:
        denom = Decimal("1") - rate - context.minimum_margin_rate
        if denom <= 0:
            return None, "INVALID_ECONOMIC_INPUT", ("margin_plus_proportional_costs_exceed_price",)
        candidates.append(_q(fixed / denom))
    if not candidates:
        denom = Decimal("1") - rate
        candidates.append(_q(fixed / denom))

    return MoneyAmount(max(candidates), context.currency), "OK", ()


def compute_breakdown(
    context: PriceProtectionContext,
    *,
    at_price: Decimal,
    minimum_allowed: MoneyAmount | None,
) -> PriceProtectionBreakdown:
    """Explainable cost breakdown at one candidate price (spec section 3)."""
    commission = _q(context.commission_fixed + context.commission_rate * at_price)
    acquiring = _q(context.acquiring_fixed + context.acquiring_rate * at_price)
    advertising = _q(context.advertising_fixed + context.advertising_rate * at_price)
    tax = _q(context.tax_fixed + context.tax_rate * at_price)
    purchase = Decimal(str(context.purchase_cost or 0))
    total_before_profit = _q(
        purchase
        + context.additional_unit_cost
        + commission
        + context.logistics_cost
        + context.last_mile_cost
        + context.fulfillment_cost
        + context.storage_cost
        + context.return_allowance
        + acquiring
        + advertising
        + context.packaging_cost
        + context.other_costs
        + tax
    )
    profit_floor = context.minimum_profit_amount if context.minimum_profit_amount is not None else Decimal("0")
    margin_floor = _q(context.minimum_margin_rate * at_price) if context.minimum_margin_rate is not None else Decimal("0")
    required_profit = max(profit_floor, margin_floor)
    expected_profit = _q(at_price - total_before_profit)
    expected_margin = _q((expected_profit / at_price) * Decimal("100")) if at_price else None
    return PriceProtectionBreakdown(
        at_price=MoneyAmount(at_price, context.currency),
        purchase_cost=purchase,
        additional_unit_cost=context.additional_unit_cost,
        marketplace_commission=commission,
        logistics=context.logistics_cost,
        last_mile=context.last_mile_cost,
        fulfillment=context.fulfillment_cost,
        storage=context.storage_cost,
        return_allowance=context.return_allowance,
        acquiring=acquiring,
        advertising=advertising,
        packaging=context.packaging_cost,
        tax=tax,
        other_costs=context.other_costs,
        required_profit=required_profit,
        total_cost_before_profit=total_before_profit,
        minimum_profit_required=required_profit,
        minimum_allowed_price=minimum_allowed,
        expected_profit=expected_profit,
        expected_margin=expected_margin,
    )


def classify_profitability(*, context: PriceProtectionContext, breakdown: PriceProtectionBreakdown) -> str:
    """Canonical profitability vocabulary (spec section 5)."""
    if breakdown.expected_profit < 0:
        return PROFITABILITY_LOSS_MAKING
    profit_floor = context.minimum_profit_amount if context.minimum_profit_amount is not None else Decimal("0")
    margin_floor = (
        _q(context.minimum_margin_rate * breakdown.at_price.amount) if context.minimum_margin_rate is not None else Decimal("0")
    )
    if breakdown.expected_profit < max(profit_floor, margin_floor):
        return PROFITABILITY_BELOW_TARGET_MARGIN if margin_floor > profit_floor else PROFITABILITY_BELOW_MINIMUM_PROFIT
    return PROFITABILITY_SAFE


@dataclass(frozen=True)
class PriceProtectionDecision:
    """One structured, explainable price-write decision (spec sections 3
    and 6). Never a bare boolean -- always answers "why"."""

    tenant_id: str
    provider: str
    sku: str
    outcome: str
    reason_codes: tuple[str, ...]
    profitability_status: str
    proposed_price: MoneyAmount
    current_price: MoneyAmount | None
    minimum_allowed_price: MoneyAmount | None
    breakdown_at_proposed: PriceProtectionBreakdown | None
    breakdown_at_minimum: PriceProtectionBreakdown | None
    delta_vs_minimum: Decimal | None
    delta_vs_current_abs: Decimal | None
    delta_vs_current_pct: Decimal | None
    policy_id: str
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "evidence", tuple(self.evidence))


def evaluate_price_decision(
    *,
    context: PriceProtectionContext,
    policy: PriceProtectionPolicy,
    proposed_price: MoneyAmount,
    current_price: MoneyAmount | None = None,
) -> PriceProtectionDecision:
    """The canonical Price Decision Engine (spec section 6/7).

    Order of evaluation (each is a hard short-circuit):
      1. Currency mismatch -> BLOCK (never invents an FX conversion).
      2. Missing/invalid economic input, or mathematically impossible
         proportional cost stack -> BLOCK.
      3. ``proposed_price < minimum_allowed_price`` (and
         ``policy.hard_floor_enabled``) -> BLOCK. This is the HARD safety
         floor (spec section 7) -- unlike every other outcome here, the
         caller (``MarketplacePlatform.write_price``) must never let
         ``approved_write=True`` override this branch.
      4. Policy-configured limits (max drop/increase percent, critical
         change percent, mandatory decrease review) -> REQUIRE_APPROVAL.
         These are economically SAFE changes (already passed step 3) that
         still warrant human review; the existing ``approved_write`` flag
         is what actually authorizes them.
      5. Otherwise -> ALLOW.
    """
    if proposed_price.currency != context.currency or (current_price is not None and current_price.currency != context.currency):
        return PriceProtectionDecision(
            tenant_id=context.tenant_id, provider=context.provider, sku=context.sku,
            outcome=PRICE_DECISION_BLOCK, reason_codes=(REASON_CURRENCY_MISMATCH,),
            profitability_status=PROFITABILITY_INVALID_ECONOMICS,
            proposed_price=proposed_price, current_price=current_price,
            minimum_allowed_price=None, breakdown_at_proposed=None, breakdown_at_minimum=None,
            delta_vs_minimum=None, delta_vs_current_abs=None, delta_vs_current_pct=None,
            policy_id=policy.policy_id, evidence=("currency_mismatch",),
        )

    min_price, status, problems = calculate_minimum_allowed_price(context)
    if status != "OK":
        return PriceProtectionDecision(
            tenant_id=context.tenant_id, provider=context.provider, sku=context.sku,
            outcome=PRICE_DECISION_BLOCK, reason_codes=(REASON_INVALID_ECONOMIC_INPUT,),
            profitability_status=PROFITABILITY_INVALID_ECONOMICS,
            proposed_price=proposed_price, current_price=current_price,
            minimum_allowed_price=None, breakdown_at_proposed=None, breakdown_at_minimum=None,
            delta_vs_minimum=None, delta_vs_current_abs=None, delta_vs_current_pct=None,
            policy_id=policy.policy_id, evidence=problems,
        )

    breakdown_at_proposed = compute_breakdown(context, at_price=proposed_price.amount, minimum_allowed=min_price)
    breakdown_at_minimum = compute_breakdown(context, at_price=min_price.amount, minimum_allowed=min_price)
    profitability_status = classify_profitability(context=context, breakdown=breakdown_at_proposed)

    delta_vs_minimum = _q(proposed_price.amount - min_price.amount)
    delta_vs_current_abs = _q(proposed_price.amount - current_price.amount) if current_price is not None else None
    delta_vs_current_pct = (
        _q(abs(delta_vs_current_abs) / current_price.amount * Decimal("100"))
        if current_price is not None and current_price.amount and delta_vs_current_abs is not None
        else None
    )

    def _decision(outcome: str, reason_codes: tuple[str, ...], evidence: tuple[str, ...] = ()) -> PriceProtectionDecision:
        return PriceProtectionDecision(
            tenant_id=context.tenant_id, provider=context.provider, sku=context.sku,
            outcome=outcome, reason_codes=reason_codes,
            profitability_status=profitability_status,
            proposed_price=proposed_price, current_price=current_price,
            minimum_allowed_price=min_price,
            breakdown_at_proposed=breakdown_at_proposed, breakdown_at_minimum=breakdown_at_minimum,
            delta_vs_minimum=delta_vs_minimum, delta_vs_current_abs=delta_vs_current_abs,
            delta_vs_current_pct=delta_vs_current_pct,
            policy_id=policy.policy_id, evidence=evidence,
        )

    if policy.hard_floor_enabled and proposed_price.amount < min_price.amount:
        codes = [REASON_BELOW_MINIMUM_ALLOWED_PRICE]
        if profitability_status == PROFITABILITY_LOSS_MAKING:
            codes.insert(0, REASON_LOSS_MAKING)
        elif profitability_status == PROFITABILITY_BELOW_TARGET_MARGIN:
            codes.insert(0, REASON_BELOW_MINIMUM_MARGIN)
        elif profitability_status == PROFITABILITY_BELOW_MINIMUM_PROFIT:
            codes.insert(0, REASON_BELOW_MINIMUM_PROFIT)
        return _decision(
            PRICE_DECISION_BLOCK, tuple(codes),
            evidence=(f"minimum_allowed={min_price.amount}", f"proposed={proposed_price.amount}"),
        )

    approval_reasons: list[str] = []
    if current_price is not None and current_price.amount > 0:
        if proposed_price.amount < current_price.amount:
            drop_pct = _q((current_price.amount - proposed_price.amount) / current_price.amount * Decimal("100"))
            if policy.maximum_price_drop_percent is not None and drop_pct > policy.maximum_price_drop_percent:
                approval_reasons.append(REASON_PRICE_DROP_LIMIT_EXCEEDED)
            if policy.require_approval_for_price_decrease:
                approval_reasons.append(REASON_CRITICAL_PRICE_CHANGE)
            if (
                policy.critical_change_percent is not None
                and policy.require_approval_for_critical_change
                and drop_pct > policy.critical_change_percent
            ):
                approval_reasons.append(REASON_CRITICAL_PRICE_CHANGE)
        elif proposed_price.amount > current_price.amount:
            increase_pct = _q((proposed_price.amount - current_price.amount) / current_price.amount * Decimal("100"))
            if policy.maximum_price_increase_percent is not None and increase_pct > policy.maximum_price_increase_percent:
                approval_reasons.append(REASON_PRICE_INCREASE_LIMIT_EXCEEDED)
            if (
                policy.critical_change_percent is not None
                and policy.require_approval_for_critical_change
                and increase_pct > policy.critical_change_percent
            ):
                approval_reasons.append(REASON_CRITICAL_PRICE_CHANGE)

    if approval_reasons:
        deduped: list[str] = []
        for reason in approval_reasons:
            if reason not in deduped:
                deduped.append(reason)
        return _decision(PRICE_DECISION_REQUIRE_APPROVAL, tuple(deduped))

    return _decision(PRICE_DECISION_ALLOW, ())
