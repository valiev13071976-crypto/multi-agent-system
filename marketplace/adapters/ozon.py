"""Ozon fixture adapter — live=false. Distinct capability profile."""

from __future__ import annotations

from marketplace.adapters.base import FakeAdapterState, FakeMarketplaceAdapter
from marketplace.models import (
    CAP_ANALYTICS_READ,
    CAP_CARD_CREATE,
    CAP_CARD_UPDATE,
    CAP_COMMISSION_READ,
    CAP_ORDER_READ,
    CAP_ORDER_STATUS_WRITE,
    CAP_PRICE_READ,
    CAP_PRICE_WRITE,
    CAP_PROMOTION_READ,
    CAP_PROMOTION_WRITE,
    CAP_REVIEW_READ,
    CAP_STOCK_READ,
    CAP_STOCK_WRITE,
    PROVIDER_OZON,
    ProviderMediaProfile,
)

# Ozon: promotion write supported; competitor read NOT supported in this fixture profile.
OZON_CAPS = frozenset(
    {
        "CATALOG_READ",
        CAP_CARD_CREATE,
        CAP_CARD_UPDATE,
        CAP_PRICE_READ,
        CAP_PRICE_WRITE,
        CAP_STOCK_READ,
        CAP_STOCK_WRITE,
        CAP_PROMOTION_READ,
        CAP_PROMOTION_WRITE,
        CAP_ORDER_READ,
        CAP_ORDER_STATUS_WRITE,
        CAP_REVIEW_READ,
        # no CAP_REVIEW_REPLY — capability difference vs WB
        CAP_ANALYTICS_READ,
        CAP_COMMISSION_READ,
    }
)

OZON_PROFILE = {
    "provider": PROVIDER_OZON,
    "live": False,
    "category_map": {"phones": "ozon-cat-electronics-phones"},
    "attribute_map": {"color": "Color", "brand": "BrandName"},
    "required_attributes": ("brand",),
    "media": ProviderMediaProfile(PROVIDER_OZON, max_images=15, min_width=1000, min_height=1000, aspect_ratio="1:1"),
    "fulfillment_schemes": ("fbo", "fbs"),
}


class OzonAdapter(FakeMarketplaceAdapter):
    def __init__(self, state: FakeAdapterState | None = None):
        super().__init__(provider=PROVIDER_OZON, caps=OZON_CAPS, state=state)

    def profile(self) -> dict:
        return dict(OZON_PROFILE)
