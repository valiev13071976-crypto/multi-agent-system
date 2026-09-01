"""Ozon mapping and price floor via Marketplace Platform."""

from __future__ import annotations

from decimal import Decimal

from integrations.ozon.catalog import OzonCatalogStore
from integrations.ozon.errors import OzonAmbiguousTargetError, OzonFulfillmentBoundaryError, OzonNotFoundError, OzonPriceFloorError
from marketplace.economics import calculate_minimum_allowed_price
from marketplace.models import MarketplaceCommissionObservation, MarketplaceMinPricePolicy, PROVIDER_OZON


def build_preview(*, operation: str, before: dict | None, after: dict) -> dict:
    return {"operation": operation, "before": before or {}, "after": after, "safe": True}


def resolve_card_target(
    store: OzonCatalogStore,
    *,
    tenant_id: str,
    product_id: int | str = "",
    offer_id: str = "",
    seller_article: str = "",
    sku: str = "",
    barcode: str = "",
    panda_product_id: str = "",
    name: str = "",
) -> dict:
    result = store.lookup(
        tenant_id=tenant_id,
        product_id=product_id,
        offer_id=offer_id,
        seller_article=seller_article,
        sku=sku,
        barcode=barcode,
        panda_product_id=panda_product_id,
        name=name,
    )
    if isinstance(result, list):
        raise OzonAmbiguousTargetError("ambiguous_card_target")
    if not result:
        raise OzonNotFoundError("card_not_found")
    return result


def map_category(*, canonical_category_id: str, category_map: dict | None = None) -> str:
    mapping = category_map or {"phones": "oz-cat-phones", "accessories": "oz-cat-accessories"}
    subject = mapping.get(canonical_category_id)
    if not subject:
        raise OzonNotFoundError("category_mapping_missing")
    return subject


def selective_rows(*, all_rows: list[dict], selected: list[str]) -> list[dict]:
    if not selected:
        return []
    sel = {s.casefold() for s in selected}
    return [
        r
        for r in all_rows
        if str(r.get("sku") or r.get("seller_article") or r.get("offer_id") or "").casefold() in sel
        or str(r.get("product_id") or "").casefold() in sel
    ]


def assert_fulfillment_warehouse(*, fulfillment: str, warehouse: str) -> None:
    from integrations.ozon.catalog import FULFILLMENT_WAREHOUSES

    allowed = FULFILLMENT_WAREHOUSES.get(fulfillment.upper(), set())
    if warehouse not in allowed:
        raise OzonFulfillmentBoundaryError(f"warehouse_not_valid_for_{fulfillment.lower()}")


def enforce_price_floor(
    *,
    store: OzonCatalogStore,
    tenant_id: str,
    seller_article: str,
    proposed_price: Decimal,
) -> dict:
    """Reuse Marketplace Platform minimum-price policy — adapter does not invent formulas."""
    card, _ = store.resolve_target(tenant_id=tenant_id, seller_article=seller_article)
    if not card:
        raise OzonNotFoundError("price_target_not_found")
    purchase = Decimal(str(card.get("purchase_cost") or "0"))
    policy = MarketplaceMinPricePolicy(policy_id="ozon_integration_fixture")
    commission = MarketplaceCommissionObservation(
        observation_id="ozon-integration-comm",
        provider=PROVIDER_OZON,
        category=str(card.get("category_id") or "default"),
        rate=Decimal("0.15"),
        fixed_fee=Decimal("0"),
    )
    min_allowed, status, evidence = calculate_minimum_allowed_price(
        purchase_cost=purchase,
        commission=commission,
        logistics=Decimal("100"),
        acquiring_rate=Decimal("0.02"),
        policy=policy,
    )
    if min_allowed is None:
        return {"allowed": True, "floor": None, "status": status, "evidence": evidence}
    if proposed_price < min_allowed.amount:
        raise OzonPriceFloorError(f"proposed_below_floor:{proposed_price}<{min_allowed.amount}")
    return {"allowed": True, "floor": str(min_allowed.amount), "status": status, "evidence": evidence}
