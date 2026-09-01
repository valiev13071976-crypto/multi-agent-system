"""Wildberries fixture adapter — live=false."""

from __future__ import annotations

from marketplace.adapters.base import FakeAdapterState, FakeMarketplaceAdapter
from marketplace.models import (
    CAP_ANALYTICS_READ,
    CAP_CARD_CREATE,
    CAP_CARD_UPDATE,
    CAP_COMMISSION_READ,
    CAP_COMPETITOR_READ,
    CAP_ORDER_READ,
    CAP_PRICE_READ,
    CAP_PRICE_WRITE,
    CAP_PROMOTION_READ,
    CAP_REVIEW_READ,
    CAP_REVIEW_REPLY,
    CAP_STOCK_READ,
    CAP_STOCK_WRITE,
    PROVIDER_WILDBERRIES,
    ProviderMediaProfile,
)

WB_CAPS = frozenset(
    {
        "CATALOG_READ",
        CAP_CARD_CREATE,
        CAP_CARD_UPDATE,
        CAP_PRICE_READ,
        CAP_PRICE_WRITE,
        CAP_STOCK_READ,
        CAP_STOCK_WRITE,
        CAP_PROMOTION_READ,
        CAP_ORDER_READ,
        CAP_REVIEW_READ,
        CAP_REVIEW_REPLY,
        CAP_ANALYTICS_READ,
        CAP_COMMISSION_READ,
        CAP_COMPETITOR_READ,
    }
)

WB_PROFILE = {
    "provider": PROVIDER_WILDBERRIES,
    "live": False,
    "category_map": {"phones": "wb-cat-phones"},
    "attribute_map": {"color": "Color", "brand": "Brand"},
    "required_attributes": ("brand", "color"),
    "media": ProviderMediaProfile(PROVIDER_WILDBERRIES, max_images=30, min_width=900, min_height=1200, aspect_ratio="3:4"),
    "fulfillment_schemes": ("marketplace", "seller"),
}


class WildberriesAdapter(FakeMarketplaceAdapter):
    def __init__(self, state: FakeAdapterState | None = None):
        super().__init__(provider=PROVIDER_WILDBERRIES, caps=WB_CAPS, state=state)

    def profile(self) -> dict:
        return dict(WB_PROFILE)
