"""Yandex Market fixture adapter — live=false. Distinct capabilities."""

from __future__ import annotations

from marketplace.adapters.base import FakeAdapterState, FakeMarketplaceAdapter
from marketplace.models import (
    CAP_ANALYTICS_READ,
    CAP_CARD_CREATE,
    CAP_CARD_UPDATE,
    CAP_COMMISSION_READ,
    CAP_ORDER_READ,
    CAP_PRICE_READ,
    CAP_STOCK_READ,
    CAP_STOCK_WRITE,
    CAP_REVIEW_READ,
    PROVIDER_YANDEX_MARKET,
    ProviderMediaProfile,
)

# Yandex: NO price write in this fixture (forces operator alert path).
YANDEX_CAPS = frozenset(
    {
        "CATALOG_READ",
        CAP_CARD_CREATE,
        CAP_CARD_UPDATE,
        CAP_PRICE_READ,
        # CAP_PRICE_WRITE intentionally absent
        CAP_STOCK_READ,
        CAP_STOCK_WRITE,
        CAP_ORDER_READ,
        CAP_REVIEW_READ,
        CAP_ANALYTICS_READ,
        CAP_COMMISSION_READ,
    }
)

YANDEX_PROFILE = {
    "provider": PROVIDER_YANDEX_MARKET,
    "live": False,
    "category_map": {"phones": "ym-cat-phones"},
    "attribute_map": {"color": "color", "brand": "vendor", "weight": "weight"},
    "required_attributes": ("brand", "color", "weight"),
    "media": ProviderMediaProfile(PROVIDER_YANDEX_MARKET, max_images=20, min_width=800, min_height=800, aspect_ratio="1:1"),
    "fulfillment_schemes": ("dbs", "fby"),
}


class YandexMarketAdapter(FakeMarketplaceAdapter):
    def __init__(self, state: FakeAdapterState | None = None):
        super().__init__(provider=PROVIDER_YANDEX_MARKET, caps=YANDEX_CAPS, state=state)

    def profile(self) -> dict:
        return dict(YANDEX_PROFILE)
