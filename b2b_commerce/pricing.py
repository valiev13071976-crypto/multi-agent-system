"""Customer-facing commercial pricing — supplier cost != customer price."""

from __future__ import annotations

from decimal import Decimal

from b2b_commerce.errors import B2B_MARGIN_POLICY_DENIED, B2B_QUOTE_APPROVAL_REQUIRED, B2BCommerceError
from b2b_commerce.platform_models import (
    QUOTE_ALLOW,
    QUOTE_DENY,
    QUOTE_REQUIRE_APPROVAL,
    VAT_INCLUDED,
    VAT_UNKNOWN,
    money_str,
    parse_money,
)
from b2b_commerce.policy import DEFAULT_MARGIN_FLOOR


def select_unit_price_for_quantity(
    *,
    base_unit_price: str,
    quantity: int,
    tiers: tuple[dict, ...] = (),
) -> Decimal:
    price = parse_money(base_unit_price)
    if tiers:
        for tier in tiers:
            min_q = int(tier.get("min_qty") or 0)
            max_q = tier.get("max_qty")
            if quantity >= min_q and (max_q is None or quantity <= int(max_q)):
                price = parse_money(tier["unit_price"])
    return price


def compute_customer_quote_lines(
    *,
    items: list[dict],
    margin_floor: Decimal = Decimal(str(DEFAULT_MARGIN_FLOOR)),
    discount_pct: Decimal = Decimal("0"),
    discount_ceiling: Decimal = Decimal("100"),
    vat_rate: Decimal | None = None,
    vat_status: str = VAT_UNKNOWN,
) -> dict:
    lines = []
    subtotal = Decimal("0")
    for item in items:
        qty = int(item["quantity"])
        supplier_cost = parse_money(item.get("supplier_cost") or item["unit_price"])
        margin = parse_money(item.get("margin_pct") or "0.20")
        unit = supplier_cost * (Decimal("1") + margin)
        unit = select_unit_price_for_quantity(
            base_unit_price=str(unit),
            quantity=qty,
            tiers=tuple(item.get("tiers") or ()),
        )
        line_subtotal = unit * qty
        subtotal += line_subtotal
        lines.append(
            {
                "product_id": item.get("product_id", ""),
                "quantity": qty,
                "unit_price": money_str(unit),
                "line_subtotal": money_str(line_subtotal),
                "supplier_cost": money_str(supplier_cost),
            }
        )

    discount = min(discount_pct, discount_ceiling)
    discount_amount = (subtotal * discount / Decimal("100")).quantize(Decimal("0.01"))
    net = subtotal - discount_amount
    vat_amount = Decimal("0")
    if vat_status == VAT_INCLUDED and vat_rate is not None:
        vat_amount = (net * vat_rate / (Decimal("100") + vat_rate)).quantize(Decimal("0.01"))
    elif vat_rate is not None:
        vat_amount = (net * vat_rate / Decimal("100")).quantize(Decimal("0.01"))
        net = net + vat_amount
    total = net if vat_status == VAT_INCLUDED else net

    approval = QUOTE_ALLOW
    for line in lines:
        supplier_cost = parse_money(line["supplier_cost"])
        unit = parse_money(line["unit_price"])
        if supplier_cost > 0:
            actual_margin = (unit - supplier_cost) / supplier_cost
            if actual_margin < margin_floor:
                approval = QUOTE_DENY
    if discount > Decimal("10") and approval != QUOTE_DENY:
        approval = QUOTE_REQUIRE_APPROVAL

    return {
        "lines": lines,
        "subtotal": money_str(subtotal),
        "discount": money_str(discount_amount),
        "vat_amount": money_str(vat_amount),
        "total": money_str(total),
        "approval_status": approval,
    }


def validate_discount_request(*, requested_discount: Decimal, ceiling: Decimal) -> str:
    if requested_discount > ceiling:
        return QUOTE_REQUIRE_APPROVAL
    return QUOTE_ALLOW


def customer_safe_projection(quote_payload: dict) -> dict:
    safe = dict(quote_payload)
    safe.pop("supplier_cost", None)
    safe.pop("margin", None)
    safe.pop("internal_notes", None)
    lines = []
    for line in safe.get("items") or safe.get("lines") or []:
        row = dict(line)
        row.pop("supplier_cost", None)
        row.pop("margin", None)
        lines.append(row)
    if "items" in safe:
        safe["items"] = lines
    if "lines" in safe:
        safe["lines"] = lines
    return safe
