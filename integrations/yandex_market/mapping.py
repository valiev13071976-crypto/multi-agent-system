"""Yandex Market mapping and price floor via Marketplace Platform."""

from __future__ import annotations

from decimal import Decimal

from integrations.yandex_market.catalog import YandexMarketCatalogStore
from integrations.yandex_market.errors import (
    YandexMarketAmbiguousTargetError,
    YandexMarketFulfillmentBoundaryError,
    YandexMarketNotFoundError,
    YandexMarketPriceFloorError,
    YandexMarketScopeError,
)
from marketplace.economics import calculate_minimum_allowed_price
from marketplace.models import MarketplaceCommissionObservation, MarketplaceMinPricePolicy, PROVIDER_YANDEX_MARKET


def build_preview(*, operation: str, before: dict | None, after: dict) -> dict:
    return {"operation": operation, "before": before or {}, "after": after, "safe": True}


def resolve_offer_target(
    store: YandexMarketCatalogStore,
    *,
    tenant_id: str,
    business_id: int | str = "",
    campaign_id: str = "",
    offer_id: str = "",
    shop_sku: str = "",
    market_sku: str = "",
    barcode: str = "",
    panda_product_id: str = "",
    name: str = "",
) -> dict:
    scope = store.business_scope(tenant_id)
    bid = business_id or scope["business_id"]
    cid = campaign_id or scope["default_campaign"]
    if business_id and str(business_id) != str(scope["business_id"]):
        raise YandexMarketScopeError("business_scope_mismatch")
    if campaign_id and campaign_id != scope["default_campaign"] and not business_id:
        raise YandexMarketScopeError("campaign_scope_mismatch")
    result = store.lookup(
        tenant_id=tenant_id,
        business_id=bid,
        campaign_id=cid,
        offer_id=offer_id,
        shop_sku=shop_sku,
        market_sku=market_sku,
        barcode=barcode,
        panda_product_id=panda_product_id,
        name=name,
    )
    if isinstance(result, list):
        raise YandexMarketAmbiguousTargetError("ambiguous_offer_target")
    if not result:
        raise YandexMarketNotFoundError("offer_not_found")
    return result


def map_category(*, canonical_category_id: str, category_map: dict | None = None) -> str:
    mapping = category_map or {"phones": "ym-cat-phones", "accessories": "ym-cat-accessories"}
    subject = mapping.get(canonical_category_id)
    if not subject:
        raise YandexMarketNotFoundError("category_mapping_missing")
    return subject


def selective_rows(*, all_rows: list[dict], selected: list[str]) -> list[dict]:
    if not selected:
        return []
    sel = {s.casefold() for s in selected}
    return [
        r
        for r in all_rows
        if str(r.get("shop_sku") or r.get("sku") or r.get("offer_id") or "").casefold() in sel
        or str(r.get("product_id") or r.get("market_sku") or "").casefold() in sel
    ]


def assert_fulfillment_warehouse(*, fulfillment: str, warehouse: str) -> None:
    from integrations.yandex_market.catalog import FULFILLMENT_WAREHOUSES

    allowed = FULFILLMENT_WAREHOUSES.get(fulfillment.upper(), set())
    if warehouse not in allowed:
        raise YandexMarketFulfillmentBoundaryError(f"warehouse_not_valid_for_{fulfillment.lower()}")


def enforce_price_floor(
    *,
    store: YandexMarketCatalogStore,
    tenant_id: str,
    shop_sku: str,
    proposed_price: Decimal,
    campaign_id: str = "",
) -> dict:
    offer, _ = store.resolve_target(tenant_id=tenant_id, shop_sku=shop_sku, campaign_id=campaign_id)
    if not offer:
        raise YandexMarketNotFoundError("price_target_not_found")
    purchase = Decimal(str(offer.get("purchase_cost") or "0"))
    policy = MarketplaceMinPricePolicy(policy_id="yandex_market_integration_fixture")
    commission = MarketplaceCommissionObservation(
        observation_id="ym-integration-comm",
        provider=PROVIDER_YANDEX_MARKET,
        category=str(offer.get("category_id") or "default"),
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
        raise YandexMarketPriceFloorError(f"proposed_below_floor:{proposed_price}<{min_allowed.amount}")
    return {"allowed": True, "floor": str(min_allowed.amount), "status": status, "evidence": evidence}
