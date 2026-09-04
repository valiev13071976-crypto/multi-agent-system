"""Block 11 — Product Economics / Margin Intelligence (Decimal-only, offline)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping

from data_intel.cleaning import clean_text, normalize_decimal_string

# Provenance
PROV_USER = "USER_PROVIDED"
PROV_FILE = "FILE_PROVIDED"
PROV_CONFIGURED = "CONFIGURED"
PROV_DERIVED = "DERIVED"
PROV_UNKNOWN = "UNKNOWN"

# Completeness
COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
INSUFFICIENT_INPUT = "INSUFFICIENT_INPUT"

# Price safety decisions
DECISION_ALLOW = "ALLOW"
DECISION_WARN = "WARN"
DECISION_DENY = "DENY"
DECISION_REQUIRE_REVIEW = "REQUIRE_REVIEW"

# Discount ownership
DISCOUNT_SELLER = "SELLER"
DISCOUNT_PLATFORM = "PLATFORM"
DISCOUNT_UNKNOWN = "UNKNOWN"

# Channels
CHANNEL_SITE = "SITE"
CHANNEL_WB = "WILDBERRIES"
CHANNEL_OZON = "OZON"
CHANNEL_YANDEX = "YANDEX_MARKET"
CHANNEL_CUSTOM = "CUSTOM"

VAT_INCLUDED = "included"
VAT_EXCLUDED = "excluded"
VAT_NONE = "none"

MONEY_SCALE = Decimal("0.01")
PCT_SCALE = Decimal("0.01")


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _dec(value) -> Decimal | None:
    text = normalize_decimal_string(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _pct(value) -> Decimal | None:
    d = _dec(value)
    if d is None:
        return None
    if d > 1:
        return d / Decimal("100")
    return d


def _row_val(row: dict, *keys: str):
    for k in keys:
        if k in row and row[k] is not None and clean_text(row[k]) is not None:
            return row[k]
    return None


@dataclass(frozen=True)
class EconomicsPolicy:
    """Tenant-scoped floor rules — rates must be supplied per row/channel, not hardcoded."""

    currency: str = "RUB"
    minimum_margin_pct: Decimal = Decimal("10")
    minimum_absolute_profit: Decimal | None = None
    minimum_roi_pct: Decimal | None = None
    warn_margin_pct: Decimal = Decimal("5")
    rounding_scale: int = 2
    vat_mode_default: str = VAT_INCLUDED
    tax_rate: Decimal | None = None
    tax_mode: str = "on_contribution"


@dataclass
class EconomicsInput:
    product_id: str = ""
    sku: str = ""
    article: str = ""
    product_name: str = ""
    channel: str = CHANNEL_SITE
    currency: str = "RUB"
    purchase_price: Decimal | None = None
    purchase_price_prov: str = PROV_UNKNOWN
    selling_price: Decimal | None = None
    selling_price_prov: str = PROV_UNKNOWN
    selling_price_vat_mode: str = VAT_INCLUDED
    vat_rate: Decimal | None = None
    vat_rate_prov: str = PROV_UNKNOWN
    commission_rate: Decimal | None = None
    commission_fixed: Decimal | None = None
    commission_prov: str = PROV_UNKNOWN
    commission_basis: str = "effective_price"
    acquiring_rate: Decimal | None = None
    acquiring_fixed: Decimal | None = None
    acquiring_prov: str = PROV_UNKNOWN
    logistics_cost: Decimal | None = None
    logistics_prov: str = PROV_UNKNOWN
    fulfillment_cost: Decimal | None = None
    fulfillment_prov: str = PROV_UNKNOWN
    storage_cost: Decimal | None = None
    storage_prov: str = PROV_UNKNOWN
    advertising_cost: Decimal | None = None
    advertising_rate: Decimal | None = None
    advertising_prov: str = PROV_UNKNOWN
    marking_cost: Decimal | None = None
    packaging_cost: Decimal | None = None
    insurance_cost: Decimal | None = None
    other_fixed_costs: Decimal | None = None
    other_rate_costs: Decimal | None = None
    discount_rate: Decimal | None = None
    discount_amount: Decimal | None = None
    discount_ownership: str = DISCOUNT_UNKNOWN
    seller_subsidy: Decimal | None = None
    marketplace_subsidy: Decimal | None = None
    tax_rate: Decimal | None = None
    tax_mode: str = ""
    quantity: Decimal = Decimal("1")
    source_provenance: dict = field(default_factory=dict)


def parse_economics_row(row: dict, *, policy: EconomicsPolicy, channel: str = CHANNEL_SITE) -> EconomicsInput:
    """Map normalized spreadsheet row → canonical economics input."""

    def money(*keys, prov=PROV_FILE):
        v = _row_val(row, *keys)
        if v is None:
            return None, PROV_UNKNOWN
        d = _dec(v)
        return d, prov if d is not None else PROV_UNKNOWN

    def rate(*keys, prov=PROV_FILE):
        v = _row_val(row, *keys)
        if v is None:
            return None, PROV_UNKNOWN
        d = _pct(v)
        return d, prov if d is not None else PROV_UNKNOWN

    purchase, pp = money("purchase_price", "cost", "закупка", "цена_закупки")
    selling, sp = money("selling_price", "price", "цена_продажи", "продажная_цена")
    comm_r, cr = rate("commission_rate", "комиссия", "marketplace_commission_rate")
    comm_f, cf = money("commission_fixed", "комиссия_фикс")
    acq_r, ar = rate("acquiring_rate", "эквайринг")
    acq_f, af = money("acquiring_fixed")
    logi, lp = money("logistics_cost", "logistics", "логистика")
    ful, fp = money("fulfillment_cost", "fulfillment")
    stor, stp = money("storage_cost", "storage", "хранение")
    adv_c, apc = money("advertising_cost", "advertising", "реклама")
    adv_r, apr = rate("advertising_rate", "advertising_pct")
    vat, vp = rate("vat_rate", "ндс", "vat")
    disc_r, dr = rate("discount_rate", "discount", "скидка")
    disc_a, da = money("discount_amount", "discount_fixed")
    ownership_raw = clean_text(_row_val(row, "discount_ownership", "subsidy_owner")) or DISCOUNT_UNKNOWN
    ownership = ownership_raw.upper() if ownership_raw else DISCOUNT_UNKNOWN
    if ownership not in {DISCOUNT_SELLER, DISCOUNT_PLATFORM, DISCOUNT_UNKNOWN}:
        ownership = DISCOUNT_UNKNOWN

    comm_prov = cr if comm_r is not None else (cf if comm_f is not None else PROV_UNKNOWN)
    acq_prov = ar if acq_r is not None else (af if acq_f is not None else PROV_UNKNOWN)
    adv_prov = apc if adv_c is not None else (apr if adv_r is not None else PROV_UNKNOWN)

    return EconomicsInput(
        product_id=clean_text(_row_val(row, "product_id")) or "",
        sku=clean_text(_row_val(row, "sku", "article", "артикул")) or "",
        article=clean_text(_row_val(row, "article", "артикул")) or "",
        product_name=clean_text(_row_val(row, "product_name", "name", "товар")) or "",
        channel=channel,
        currency=clean_text(_row_val(row, "currency", "валюта")) or policy.currency,
        purchase_price=purchase,
        purchase_price_prov=pp,
        selling_price=selling,
        selling_price_prov=sp,
        selling_price_vat_mode=clean_text(_row_val(row, "selling_price_vat_mode")) or policy.vat_mode_default,
        vat_rate=vat,
        vat_rate_prov=vp,
        commission_rate=comm_r,
        commission_fixed=comm_f or Decimal("0") if comm_f is not None else None,
        commission_prov=comm_prov,
        acquiring_rate=acq_r,
        acquiring_fixed=acq_f or Decimal("0") if acq_f is not None else None,
        acquiring_prov=acq_prov,
        logistics_cost=logi,
        logistics_prov=lp,
        fulfillment_cost=ful,
        fulfillment_prov=fp,
        storage_cost=stor,
        storage_prov=stp,
        advertising_cost=adv_c,
        advertising_rate=adv_r,
        advertising_prov=adv_prov,
        marking_cost=_dec(_row_val(row, "marking_cost", "marking")),
        packaging_cost=_dec(_row_val(row, "packaging_cost", "packaging")),
        insurance_cost=_dec(_row_val(row, "insurance_cost", "insurance")),
        other_fixed_costs=_dec(_row_val(row, "other_fixed_costs", "other_costs")),
        other_rate_costs=_pct(_row_val(row, "other_rate_costs")),
        discount_rate=disc_r,
        discount_amount=disc_a,
        discount_ownership=ownership,
        seller_subsidy=_dec(_row_val(row, "seller_subsidy")),
        marketplace_subsidy=_dec(_row_val(row, "marketplace_subsidy")),
        tax_rate=_pct(_row_val(row, "tax_rate")) or policy.tax_rate,
        tax_mode=clean_text(_row_val(row, "tax_mode")) or policy.tax_mode,
        quantity=_dec(_row_val(row, "quantity")) or Decimal("1"),
        source_provenance={
            "source_file": row.get("source_file") or row.get("__source_file"),
            "source_sheet": row.get("source_sheet") or row.get("__source_sheet"),
            "source_row": row.get("source_row") or row.get("__source_row"),
        },
    )


def _effective_price(inp: EconomicsInput) -> tuple[Decimal | None, list[str], str]:
    """Apply discount ownership; return (price, warnings, discount_note)."""
    if inp.selling_price is None:
        return None, ["missing_selling_price"], "none"
    price = inp.selling_price
    warnings: list[str] = []
    if inp.discount_ownership == DISCOUNT_UNKNOWN and (
        inp.discount_rate or inp.discount_amount or inp.marketplace_subsidy
    ):
        warnings.append("discount_ownership_unknown")
        return price, warnings, "unknown_ownership_partial"
    if inp.discount_ownership == DISCOUNT_PLATFORM:
        # Platform-funded: seller revenue based on list/seller price, not buyer display
        return _q(price), warnings, "platform_funded"
    eff = price
    if inp.discount_rate:
        eff = _q(eff * (Decimal("1") - inp.discount_rate))
    if inp.discount_amount:
        eff = _q(eff - inp.discount_amount)
    if inp.discount_ownership == DISCOUNT_SELLER and inp.seller_subsidy:
        eff = _q(eff - inp.seller_subsidy)
    return eff, warnings, "seller_funded"


def _vat_extract(price: Decimal, rate: Decimal | None, mode: str) -> tuple[Decimal, Decimal | None]:
    if mode == VAT_NONE or rate is None:
        return price, None
    if mode == VAT_INCLUDED:
        net = _q(price / (Decimal("1") + rate))
        vat_amt = _q(price - net)
        return net, vat_amt
    return price, _q(price * rate)


def _cost_component(value: Decimal | None, prov: str, name: str, missing: list[str]) -> Decimal:
    if value is None and prov == PROV_UNKNOWN:
        missing.append(name)
        return Decimal("0")
    return value or Decimal("0")


def calculate_economics(inp: EconomicsInput, *, policy: EconomicsPolicy | None = None) -> dict:
    """Deterministic product economics. Never labels incomplete results as net profit."""
    policy = policy or EconomicsPolicy()
    missing: list[str] = []
    warnings: list[str] = []
    provenance: dict[str, str] = {}

    if inp.purchase_price is None:
        missing.append("purchase_price")
    if inp.selling_price is None:
        missing.append("selling_price")
    if inp.currency != policy.currency:
        return {
            "status": INSUFFICIENT_INPUT,
            "completeness": INSUFFICIENT_INPUT,
            "decision": DECISION_REQUIRE_REVIEW,
            "warnings": ["currency_mismatch"],
            "missing": ["currency_conversion"],
            "profit_label": "unavailable",
        }

    eff_price, disc_warnings, disc_note = _effective_price(inp)
    warnings.extend(disc_warnings)
    if eff_price is None:
        return {
            "status": INSUFFICIENT_INPUT,
            "completeness": INSUFFICIENT_INPUT,
            "decision": DECISION_REQUIRE_REVIEW,
            "missing": missing,
            "warnings": warnings,
            "profit_label": "unavailable",
        }

    vat_mode = inp.selling_price_vat_mode or policy.vat_mode_default
    if vat_mode not in {VAT_INCLUDED, VAT_EXCLUDED, VAT_NONE}:
        warnings.append("ambiguous_vat_mode")
        vat_mode = policy.vat_mode_default

    revenue, vat_amount = _vat_extract(eff_price, inp.vat_rate, vat_mode)
    if inp.vat_rate is None and vat_mode != VAT_NONE:
        missing.append("vat_rate")
        warnings.append("vat_partial")

    basis = revenue if inp.commission_basis == "effective_price" else eff_price
    comm = Decimal("0")
    if inp.commission_rate is not None:
        comm += _q(basis * inp.commission_rate)
    if inp.commission_fixed is not None:
        comm += _q(inp.commission_fixed)
    elif inp.commission_rate is None and inp.commission_prov == PROV_UNKNOWN:
        missing.append("commission")

    acq = Decimal("0")
    if inp.acquiring_rate is not None:
        acq += _q(basis * inp.acquiring_rate)
    if inp.acquiring_fixed is not None:
        acq += _q(inp.acquiring_fixed)

    logi = _cost_component(inp.logistics_cost, inp.logistics_prov, "logistics", missing)
    ful = _cost_component(inp.fulfillment_cost, inp.fulfillment_prov, "fulfillment", missing)
    stor = _cost_component(inp.storage_cost, inp.storage_prov, "storage", missing)

    adv = Decimal("0")
    if inp.advertising_cost is not None:
        adv = _q(inp.advertising_cost)
    elif inp.advertising_rate is not None:
        adv = _q(revenue * inp.advertising_rate)
    elif inp.advertising_prov == PROV_UNKNOWN:
        missing.append("advertising")

    marking = inp.marking_cost or Decimal("0")
    packaging = inp.packaging_cost or Decimal("0")
    insurance = inp.insurance_cost or Decimal("0")
    other_fixed = inp.other_fixed_costs or Decimal("0")
    other_rate = _q(revenue * inp.other_rate_costs) if inp.other_rate_costs else Decimal("0")

    purchase = inp.purchase_price or Decimal("0")
    variable = comm + acq + logi + ful + stor + adv + marking + packaging + insurance + other_fixed + other_rate
    total_cost = _q(purchase + variable)

    gross_diff = _q(eff_price - purchase)
    contribution = _q(revenue - total_cost)
    margin_pct = _q(contribution / revenue * Decimal("100")) if revenue else None
    roi_pct = _q(contribution / purchase * Decimal("100")) if purchase and purchase > 0 else None

    tax_amount = Decimal("0")
    if inp.tax_rate is not None and inp.tax_mode == "on_contribution" and contribution > 0:
        tax_amount = _q(contribution * inp.tax_rate)
    elif inp.tax_rate is None and policy.tax_rate is not None:
        tax_amount = _q(max(Decimal("0"), contribution) * policy.tax_rate)

    profit_after_tax = _q(contribution - tax_amount)

    completeness = COMPLETE
    if missing or disc_note == "unknown_ownership_partial":
        completeness = PARTIAL if (inp.purchase_price and inp.selling_price) else INSUFFICIENT_INPUT
    if not inp.purchase_price or not inp.selling_price:
        completeness = INSUFFICIENT_INPUT

    if contribution > 0 and missing:
        profit_label = "estimated_contribution_partial"
    elif missing:
        profit_label = "partial_economics"
    elif completeness == COMPLETE:
        profit_label = "contribution_profit"
    else:
        profit_label = "gross_difference_only" if not missing else "estimated_contribution"

    min_price, floor_status, floor_missing = calculate_minimum_price(inp, policy=policy)
    decision = evaluate_price_decision(
        selling_price=eff_price,
        minimum_allowed=min_price,
        completeness=completeness,
        missing=missing,
        margin_pct=margin_pct,
        policy=policy,
    )

    for field_name, prov in (
        ("purchase_price", inp.purchase_price_prov),
        ("selling_price", inp.selling_price_prov),
        ("commission", inp.commission_prov),
        ("advertising", inp.advertising_prov),
    ):
        provenance[field_name] = prov

    return {
        "status": "OK" if completeness != INSUFFICIENT_INPUT else INSUFFICIENT_INPUT,
        "completeness": completeness,
        "product_id": inp.product_id,
        "sku": inp.sku,
        "article": inp.article,
        "product_name": inp.product_name,
        "channel": inp.channel,
        "currency": inp.currency,
        "selling_price": str(inp.selling_price),
        "effective_price": str(eff_price),
        "revenue": str(revenue),
        "purchase_price": str(purchase),
        "commission": str(comm),
        "acquiring": str(acq),
        "logistics": str(logi),
        "fulfillment": str(ful),
        "storage": str(stor),
        "advertising": str(adv),
        "other_costs": str(_q(other_fixed + other_rate + marking + packaging + insurance)),
        "vat_amount": str(vat_amount) if vat_amount is not None else None,
        "tax_amount": str(tax_amount),
        "total_costs": str(total_cost),
        "gross_difference": str(gross_diff),
        "contribution": str(contribution),
        "profit_after_tax": str(profit_after_tax),
        "margin_pct": str(margin_pct) if margin_pct is not None else None,
        "roi_pct": str(roi_pct) if roi_pct is not None else None,
        "break_even_price": str(min_price) if floor_status == "break_even_only" else None,
        "minimum_allowed_price": str(min_price) if min_price is not None else None,
        "floor_status": floor_status,
        "floor_missing": floor_missing,
        "decision": decision,
        "profit_label": profit_label,
        "discount_note": disc_note,
        "missing": missing,
        "warnings": warnings,
        "provenance": provenance,
        "formula": "contribution = revenue - (purchase + commission + acquiring + logistics + fulfillment + storage + advertising + other)",
        "note": "Not net profit unless completeness=COMPLETE and all material costs configured",
        "source_provenance": dict(inp.source_provenance),
    }


def calculate_minimum_price(
    inp: EconomicsInput,
    *,
    policy: EconomicsPolicy | None = None,
) -> tuple[Decimal | None, str, tuple[str, ...]]:
    """Minimum allowed / break-even price. No false floor when inputs incomplete."""
    policy = policy or EconomicsPolicy()
    unknown: list[str] = []
    if inp.purchase_price is None:
        unknown.append("purchase_price")

    fixed = Decimal("0")
    if inp.purchase_price is not None:
        fixed += inp.purchase_price
    if inp.commission_fixed is not None:
        fixed += inp.commission_fixed
    elif policy.minimum_margin_pct and inp.commission_prov == PROV_UNKNOWN:
        unknown.append("commission_fixed")
    for val, prov, name in (
        (inp.logistics_cost, inp.logistics_prov, "logistics"),
        (inp.fulfillment_cost, inp.fulfillment_prov, "fulfillment"),
        (inp.storage_cost, inp.storage_prov, "storage"),
    ):
        if val is not None:
            fixed += val
        elif prov == PROV_UNKNOWN:
            pass  # optional for floor unless policy requires
    if inp.advertising_cost is not None:
        fixed += inp.advertising_cost
    elif inp.advertising_prov == PROV_UNKNOWN and inp.advertising_rate is None:
        unknown.append("advertising")

    rate_stack = Decimal("0")
    if inp.commission_rate is not None:
        rate_stack += inp.commission_rate
    elif inp.commission_prov == PROV_UNKNOWN:
        unknown.append("commission_rate")
    if inp.acquiring_rate is not None:
        rate_stack += inp.acquiring_rate
    if inp.advertising_rate is not None:
        rate_stack += inp.advertising_rate
    if inp.other_rate_costs is not None:
        rate_stack += inp.other_rate_costs

    margin = policy.minimum_margin_pct / Decimal("100")
    min_profit = policy.minimum_absolute_profit
    roi_target = policy.minimum_roi_pct / Decimal("100") if policy.minimum_roi_pct else None

    if unknown:
        return None, "PRICE_FLOOR_UNAVAILABLE", tuple(unknown)

    if roi_target and inp.purchase_price and inp.purchase_price > 0:
        # price such that contribution/purchase >= roi_target
        # contribution = price*(1-rate_stack) - fixed >= purchase*roi_target
        denom = Decimal("1") - rate_stack - margin
        if denom <= 0:
            return None, "PRICE_FLOOR_UNAVAILABLE", ("invalid_rate_stack",)
        target = inp.purchase_price * (Decimal("1") + roi_target)
        min_price = _q((fixed + target) / denom)
        return min_price, "minimum_roi", ()

    if min_profit is not None:
        denom = Decimal("1") - rate_stack
        if denom <= 0:
            return None, "PRICE_FLOOR_UNAVAILABLE", ("invalid_rate_stack",)
        min_price = _q((fixed + min_profit) / denom)
        return min_price, "minimum_absolute_profit", ()

    denom = Decimal("1") - rate_stack - margin
    if denom <= 0:
        return None, "PRICE_FLOOR_UNAVAILABLE", ("invalid_rate_stack",)
    min_price = _q(fixed / denom)
    return min_price, "minimum_margin", ()


def evaluate_price_decision(
    *,
    selling_price: Decimal | None,
    minimum_allowed: Decimal | None,
    completeness: str,
    missing: list[str],
    margin_pct: Decimal | None,
    policy: EconomicsPolicy | None = None,
) -> str:
    policy = policy or EconomicsPolicy()
    material_missing = {"purchase_price", "selling_price", "commission", "commission_rate", "advertising"}
    if completeness == INSUFFICIENT_INPUT or material_missing.intersection(missing):
        if missing:
            return DECISION_REQUIRE_REVIEW
    if selling_price is None:
        return DECISION_REQUIRE_REVIEW
    if minimum_allowed is None:
        return DECISION_REQUIRE_REVIEW if missing else DECISION_WARN
    if selling_price < minimum_allowed:
        return DECISION_DENY
    warn_threshold = minimum_allowed * (Decimal("1") + policy.warn_margin_pct / Decimal("100"))
    if selling_price <= warn_threshold:
        return DECISION_WARN
    if margin_pct is not None and margin_pct < policy.minimum_margin_pct:
        return DECISION_WARN
    return DECISION_ALLOW


def apply_scenario(inp: EconomicsInput, adjustments: Mapping[str, object]) -> EconomicsInput:
    """Non-mutating what-if; returns new EconomicsInput."""
    data = {
        "product_id": inp.product_id,
        "sku": inp.sku,
        "article": inp.article,
        "product_name": inp.product_name,
        "channel": inp.channel,
        "currency": inp.currency,
        "purchase_price": inp.purchase_price,
        "purchase_price_prov": inp.purchase_price_prov,
        "selling_price": inp.selling_price,
        "selling_price_prov": inp.selling_price_prov,
        "selling_price_vat_mode": inp.selling_price_vat_mode,
        "vat_rate": inp.vat_rate,
        "vat_rate_prov": inp.vat_rate_prov,
        "commission_rate": inp.commission_rate,
        "commission_fixed": inp.commission_fixed,
        "commission_prov": inp.commission_prov,
        "acquiring_rate": inp.acquiring_rate,
        "acquiring_fixed": inp.acquiring_fixed,
        "acquiring_prov": inp.acquiring_prov,
        "logistics_cost": inp.logistics_cost,
        "logistics_prov": inp.logistics_prov,
        "fulfillment_cost": inp.fulfillment_cost,
        "fulfillment_prov": inp.fulfillment_prov,
        "storage_cost": inp.storage_cost,
        "storage_prov": inp.storage_prov,
        "advertising_cost": inp.advertising_cost,
        "advertising_rate": inp.advertising_rate,
        "advertising_prov": inp.advertising_prov,
        "discount_rate": inp.discount_rate,
        "discount_amount": inp.discount_amount,
        "discount_ownership": inp.discount_ownership,
        "tax_rate": inp.tax_rate,
        "tax_mode": inp.tax_mode,
        "source_provenance": dict(inp.source_provenance),
    }
    for k, v in adjustments.items():
        if k == "price_delta_pct" and data["selling_price"] is not None:
            raw = _dec(v)
            delta = (raw / Decimal("100")) if raw is not None and abs(raw) > 1 else (raw or Decimal("0"))
            data["selling_price"] = _q(data["selling_price"] * (Decimal("1") + delta))
            data["selling_price_prov"] = PROV_DERIVED
        elif k.endswith("_pct") or k in {"commission_rate", "acquiring_rate", "advertising_rate", "discount_rate", "vat_rate", "tax_rate"}:
            data[k.replace("_pct", "_rate") if k.endswith("_pct") else k] = _pct(v)
        elif k == "logistics_delta" and data["logistics_cost"] is not None:
            data["logistics_cost"] = _q(data["logistics_cost"] + (_dec(v) or Decimal("0")))
            data["logistics_prov"] = PROV_DERIVED
        elif hasattr(inp, k):
            data[k] = _dec(v) if k.endswith("_cost") or k.endswith("_price") else v
    return EconomicsInput(**{kk: data.get(kk, getattr(inp, kk, None)) for kk in EconomicsInput.__dataclass_fields__})


def compare_channels(
    base: EconomicsInput,
    channels: Mapping[str, dict],
    *,
    policy: EconomicsPolicy | None = None,
) -> list[dict]:
    """Multi-channel comparison with explicit per-channel config — no hardcoded rates."""
    results = []
    for ch_name, cfg in channels.items():
        merged = EconomicsInput(
            product_id=base.product_id,
            sku=base.sku,
            article=base.article,
            product_name=base.product_name,
            channel=ch_name,
            currency=base.currency,
            purchase_price=base.purchase_price,
            purchase_price_prov=base.purchase_price_prov,
            selling_price=base.selling_price,
            selling_price_prov=base.selling_price_prov,
            selling_price_vat_mode=base.selling_price_vat_mode,
            vat_rate=base.vat_rate or _pct(cfg.get("vat_rate")),
            commission_rate=_pct(cfg.get("commission_rate")),
            commission_fixed=_dec(cfg.get("commission_fixed")),
            commission_prov=PROV_CONFIGURED if cfg.get("commission_rate") is not None else PROV_UNKNOWN,
            acquiring_rate=_pct(cfg.get("acquiring_rate")),
            acquiring_prov=PROV_CONFIGURED if cfg.get("acquiring_rate") is not None else PROV_UNKNOWN,
            logistics_cost=_dec(cfg.get("logistics_cost")),
            logistics_prov=PROV_CONFIGURED if cfg.get("logistics_cost") is not None else PROV_UNKNOWN,
            fulfillment_cost=_dec(cfg.get("fulfillment_cost")),
            storage_cost=_dec(cfg.get("storage_cost")),
            advertising_cost=_dec(cfg.get("advertising_cost")),
            advertising_rate=_pct(cfg.get("advertising_rate")),
            advertising_prov=PROV_CONFIGURED if cfg.get("advertising_cost") is not None or cfg.get("advertising_rate") is not None else PROV_UNKNOWN,
            discount_rate=base.discount_rate,
            discount_amount=base.discount_amount,
            discount_ownership=base.discount_ownership,
            source_provenance=dict(base.source_provenance),
        )
        econ = calculate_economics(merged, policy=policy)
        econ["channel"] = ch_name
        results.append(econ)
    return results


def economics_batch_rows(
    rows: list[dict],
    *,
    policy: EconomicsPolicy | None = None,
    channel: str = CHANNEL_SITE,
    channel_configs: Mapping[str, dict] | None = None,
) -> dict:
    """Row-by-row economics for Excel batch workflow."""
    policy = policy or EconomicsPolicy()
    result_rows: list[dict] = []
    issues: list[dict] = []
    scenarios: list[dict] = []
    counts = {"complete": 0, "partial": 0, "insufficient": 0, "allow": 0, "warn": 0, "deny": 0, "review": 0}

    for i, row in enumerate(rows):
        inp = parse_economics_row(row, policy=policy, channel=channel)
        if channel_configs and len(channel_configs) > 1:
            multi = compare_channels(inp, channel_configs, policy=policy)
            for m in multi:
                result_rows.append(m)
                _tally(m, counts)
                _collect_issues(m, issues, i, row)
        else:
            econ = calculate_economics(inp, policy=policy)
            result_rows.append(econ)
            _tally(econ, counts)
            _collect_issues(econ, issues, i, row)

    summary = {
        "process": "product_economics",
        "rows": len(rows),
        "result_rows": len(result_rows),
        **counts,
        "profit_label_policy": "never_net_profit_when_partial",
    }
    return {"results": result_rows, "issues": issues, "scenarios": scenarios, "summary": summary}


def _tally(econ: dict, counts: dict) -> None:
    c = econ.get("completeness", "")
    if c == COMPLETE:
        counts["complete"] += 1
    elif c == PARTIAL:
        counts["partial"] += 1
    else:
        counts["insufficient"] += 1
    d = econ.get("decision", "")
    if d == DECISION_ALLOW:
        counts["allow"] += 1
    elif d == DECISION_WARN:
        counts["warn"] += 1
    elif d == DECISION_DENY:
        counts["deny"] += 1
    else:
        counts["review"] += 1


def _collect_issues(econ: dict, issues: list[dict], idx: int, row: dict) -> None:
    for m in econ.get("missing") or []:
        issues.append(
            {
                "file": row.get("source_file") or "",
                "sheet": row.get("source_sheet") or "",
                "row": row.get("source_row") or row.get("__source_row") or idx + 2,
                "column": m,
                "reason": "missing_material_input",
                "severity": "warning",
            }
        )
    if econ.get("decision") == DECISION_DENY:
        issues.append(
            {
                "file": row.get("source_file") or "",
                "sheet": row.get("source_sheet") or "",
                "row": row.get("source_row") or row.get("__source_row") or idx + 2,
                "column": "selling_price",
                "reason": "below_minimum_allowed_price",
                "severity": "error",
            }
        )


def economics_result_table(results: list[dict]) -> tuple[list[str], list[list], set[int]]:
    headers = [
        "sku",
        "article",
        "product_name",
        "channel",
        "purchase_price",
        "selling_price",
        "effective_price",
        "revenue",
        "commission",
        "acquiring",
        "logistics",
        "fulfillment",
        "storage",
        "advertising",
        "other_costs",
        "vat_amount",
        "tax_amount",
        "total_costs",
        "contribution",
        "margin_pct",
        "roi_pct",
        "minimum_allowed_price",
        "decision",
        "completeness",
        "profit_label",
        "warnings",
    ]
    text_cols = {0, 1, 2, 3, 23, 24, 25}
    body = []
    for r in results:
        body.append([r.get(h) for h in headers])
    return headers, body, text_cols
