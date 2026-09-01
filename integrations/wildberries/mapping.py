"""Wildberries mapping and price floor via Marketplace Platform."""

from __future__ import annotations

from decimal import Decimal

from integrations.wildberries.catalog import WildberriesCatalogStore
from integrations.wildberries.errors import WildberriesAmbiguousTargetError, WildberriesNotFoundError, WildberriesPriceFloorError
from marketplace.economics import calculate_minimum_allowed_price
from marketplace.models import MarketplaceCommissionObservation, MarketplaceMinPricePolicy, PROVIDER_WILDBERRIES


def build_preview(*, operation: str, before: dict | None, after: dict) -> dict:
    return {"operation": operation, "before": before or {}, "after": after, "safe": True}


def resolve_card_target(
    store: WildberriesCatalogStore,
    *,
    tenant_id: str,
    nm_id: int | str = "",
    chrt_id: int | str = "",
    seller_article: str = "",
    barcode: str = "",
    panda_product_id: str = "",
    name: str = "",
) -> dict:
    result = store.lookup(
        tenant_id=tenant_id,
        nm_id=nm_id,
        chrt_id=chrt_id,
        seller_article=seller_article,
        barcode=barcode,
        panda_product_id=panda_product_id,
        name=name,
    )
    if isinstance(result, list):
        raise WildberriesAmbiguousTargetError("ambiguous_card_target")
    if not result:
        raise WildberriesNotFoundError("card_not_found")
    return result


def map_category(*, canonical_category_id: str, category_map: dict | None = None) -> str:
    mapping = category_map or {"phones": "wb-cat-phones", "accessories": "wb-cat-accessories"}
    subject = mapping.get(canonical_category_id)
    if not subject:
        raise WildberriesNotFoundError("category_mapping_missing")
    return subject


def selective_rows(*, all_rows: list[dict], selected: list[str]) -> list[dict]:
    if not selected:
        return []
    sel = {s.casefold() for s in selected}
    return [
        r
        for r in all_rows
        if str(r.get("sku") or r.get("seller_article") or "").casefold() in sel
        or str(r.get("product_id") or "").casefold() in sel
    ]


def enforce_price_floor(
    *,
    store: WildberriesCatalogStore,
    tenant_id: str,
    seller_article: str,
    proposed_price: Decimal,
) -> dict:
    """Reuse Marketplace Platform minimum-price policy — adapter does not invent formulas."""
    card, _ = store.resolve_variant(tenant_id=tenant_id, seller_article=seller_article)
    if not card:
        raise WildberriesNotFoundError("price_target_not_found")
    purchase = Decimal(str(card.get("purchase_cost") or "0"))
    policy = MarketplaceMinPricePolicy(policy_id="wb_integration_fixture")
    commission = MarketplaceCommissionObservation(
        observation_id="wb-integration-comm",
        provider=PROVIDER_WILDBERRIES,
        category=str(card.get("subject_id") or "default"),
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
        raise WildberriesPriceFloorError(
            f"proposed_below_floor:{proposed_price}<{min_allowed.amount}"
        )
    return {"allowed": True, "floor": str(min_allowed.amount), "status": status, "evidence": evidence}
