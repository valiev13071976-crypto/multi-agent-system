"""Product Platform runtime wiring."""

from __future__ import annotations

from commerce.product_platform.service import ProductPlatformService
from commerce.store import CommerceStore


def build_product_platform_service(
    *,
    store: CommerceStore | None = None,
    product_media_service=None,
    content_intelligence_service=None,
) -> ProductPlatformService:
    commerce_store = store or CommerceStore(path=":memory:")
    return ProductPlatformService(
        store=commerce_store,
        product_media_service=product_media_service,
        content_intelligence_service=content_intelligence_service,
    )
