"""Cart / checkout contracts — revalidation before order."""

from __future__ import annotations

import uuid
from decimal import Decimal

from commerce.product_platform.errors import COMMERCE_CART_INVALID, COMMERCE_PRICE_CHANGED, ProductPlatformError
from commerce.product_platform.models import AVAIL_IN_STOCK, AVAIL_OUT_OF_STOCK, Cart, CartLine, MoneyAmount, money


def create_cart(
    *,
    tenant_id: str,
    currency: str = "RUB",
    lines: list[dict],
    customer_ref: str = "",
) -> Cart:
    built: list[CartLine] = []
    for row in lines:
        qty = money(row.get("quantity") or 1)
        if qty <= 0:
            raise ProductPlatformError(COMMERCE_CART_INVALID, "invalid_quantity")
        unit = MoneyAmount(money(row.get("unit_price") or 0), currency)
        built.append(
            CartLine(
                line_id=str(row.get("line_id") or uuid.uuid4()),
                sku=str(row.get("sku") or ""),
                product_id=str(row.get("product_id") or ""),
                quantity=qty,
                unit_price=unit,
                availability=str(row.get("availability") or AVAIL_IN_STOCK),
            )
        )
    if not built:
        raise ProductPlatformError(COMMERCE_CART_INVALID, "empty_cart")
    return Cart(
        cart_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        currency=currency,
        lines=tuple(built),
        customer_ref=customer_ref,
    )


def revalidate_checkout(
    *,
    cart: Cart,
    current_prices: dict[str, Decimal],
    current_availability: dict[str, str],
) -> dict:
    """Reprice/availability check — explicit changed lines, no silent charge."""
    changed: list[dict] = []
    for line in cart.lines:
        key = line.product_id or line.sku
        avail = current_availability.get(key, line.availability)
        if avail == AVAIL_OUT_OF_STOCK:
            changed.append({"line_id": line.line_id, "reason": "OUT_OF_STOCK", "sku": line.sku})
        price = current_prices.get(key)
        if price is not None and money(price) != line.unit_price.amount:
            changed.append(
                {
                    "line_id": line.line_id,
                    "reason": "PRICE_CHANGED",
                    "old": str(line.unit_price.amount),
                    "new": str(price),
                    "sku": line.sku,
                }
            )
    if any(c["reason"] == "PRICE_CHANGED" for c in changed):
        return {"ok": False, "code": COMMERCE_PRICE_CHANGED, "changed_lines": changed}
    if any(c["reason"] == "OUT_OF_STOCK" for c in changed):
        return {"ok": False, "code": COMMERCE_CART_INVALID, "changed_lines": changed}
    subtotal = sum((line.unit_price.amount * line.quantity for line in cart.lines), Decimal("0"))
    return {"ok": True, "subtotal": str(subtotal), "currency": cart.currency, "changed_lines": []}
