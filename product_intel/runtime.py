"""Product Intelligence runtime composition (Block 5.5)."""

from __future__ import annotations

import os

from product_intel.service import ProductIntelligenceService
from product_intel.store import InMemoryProductCatalogStore


def product_intel_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("PRODUCT_INTEL_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


class ProductIntelligenceRuntime:
    def __init__(self, *, service: ProductIntelligenceService, enabled: bool = True):
        self.service = service
        self.enabled = bool(enabled)

    def health(self) -> dict:
        return {"product_intel_status": "healthy" if self.enabled else "disabled", "enabled": self.enabled}


def build_product_intelligence_runtime(
    *,
    env: dict | None = None,
    store=None,
    data_intelligence_service=None,
    content_intelligence_service=None,
    artifact_service=None,
    observability=None,
) -> ProductIntelligenceRuntime | None:
    if not product_intel_enabled(env):
        return None
    catalog_store = store or InMemoryProductCatalogStore()
    service = ProductIntelligenceService(
        catalog_store,
        data_intelligence_service=data_intelligence_service,
        content_intelligence_service=content_intelligence_service,
        artifact_service=artifact_service,
        observability=observability,
    )
    return ProductIntelligenceRuntime(service=service, enabled=True)
