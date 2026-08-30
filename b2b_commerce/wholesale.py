"""Deterministic wholesale comparison and price change detection."""

from __future__ import annotations

import uuid
from decimal import Decimal

from b2b_commerce.errors import B2B_CURRENCY_MISMATCH, B2BCommerceError
from b2b_commerce.platform_models import (
    OFFER_STALE,
    VAT_EXCLUDED,
    VAT_INCLUDED,
    WholesaleComparison,
    WholesaleOfferVersion,
    WholesalePriceChange,
    parse_money,
)


def _normalize_price(offer: WholesaleOfferVersion, *, requested_qty: int) -> Decimal | None:
    if offer.freshness == OFFER_STALE:
        return None
    if offer.moq is not None and requested_qty < offer.moq:
        return None
    base = parse_money(offer.unit_price)
    if offer.quantity_tiers:
        selected = None
        for tier in offer.quantity_tiers:
            min_q = int(tier.get("min_qty") or 0)
            max_q = tier.get("max_qty")
            if requested_qty >= min_q and (max_q is None or requested_qty <= int(max_q)):
                selected = tier
        if selected:
            base = parse_money(selected["unit_price"])
    if offer.vat_status == VAT_EXCLUDED:
        return base
    if offer.vat_status == VAT_INCLUDED:
        return base
    return base


def compare_offers(
    offers: list[WholesaleOfferVersion],
    *,
    tenant_id: str,
    product_id: str,
    requested_quantity: int,
    preferred_supplier: str = "",
) -> WholesaleComparison:
    if not offers:
        raise B2BCommerceError("B2B_PRODUCT_UNMATCHED", "no offers")
    currency = offers[0].currency
    for offer in offers[1:]:
        if offer.currency != currency:
            raise B2BCommerceError(B2B_CURRENCY_MISMATCH)

    ranked: list[tuple[WholesaleOfferVersion, Decimal, tuple[str, ...]]] = []
    for offer in offers:
        price = _normalize_price(offer, requested_qty=requested_quantity)
        if price is None:
            continue
        components: list[str] = ["MOQ_COMPATIBLE", "FRESH"]
        if offer.supplier_id == preferred_supplier:
            components.append("PREFERRED_SUPPLIER")
        if offer.available_quantity is not None and offer.available_quantity >= requested_quantity:
            components.append("AVAILABLE")
        ranked.append((offer, price, tuple(components)))

    if not ranked:
        raise B2BCommerceError("B2B_OFFER_STALE", "no compatible offers")

    ranked.sort(key=lambda item: (item[1], 0 if "PREFERRED_SUPPLIER" in item[2] else 1))
    best_offer, _, components = ranked[0]
    return WholesaleComparison(
        comparison_id=f"cmp_{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        product_id=product_id,
        requested_quantity=requested_quantity,
        best_offer_id=best_offer.offer_id,
        ranking_reason="LOWEST_NORMALIZED_PRICE",
        components=components,
        offers=tuple(
            {
                "offer_id": o.offer_id,
                "supplier_id": o.supplier_id,
                "normalized_price": str(p),
                "currency": o.currency,
                "components": list(c),
            }
            for o, p, c in ranked
        ),
    )


def detect_price_changes(
    old_offers: list[WholesaleOfferVersion],
    new_offers: list[WholesaleOfferVersion],
    *,
    tenant_id: str,
    supplier_id: str,
) -> list[WholesalePriceChange]:
    old_map = {(o.supplier_sku or o.offer_id): o for o in old_offers}
    changes: list[WholesalePriceChange] = []
    for new in new_offers:
        key = new.supplier_sku or new.offer_id
        old = old_map.get(key)
        if not old:
            continue
        if old.currency != new.currency:
            continue
        old_p = parse_money(old.unit_price)
        new_p = parse_money(new.unit_price)
        if old_p == new_p:
            continue
        delta = new_p - old_p
        pct = (delta / old_p * Decimal("100")) if old_p else Decimal("0")
        direction = "UP" if delta > 0 else "DOWN" if delta < 0 else "UNCHANGED"
        changes.append(
            WholesalePriceChange(
                change_id=f"chg_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                offer_id=new.offer_id,
                old_price=str(old_p),
                new_price=str(new_p),
                delta_abs=str(delta),
                delta_pct=str(pct.quantize(Decimal("0.01"))),
                direction=direction,
            )
        )
    return changes
