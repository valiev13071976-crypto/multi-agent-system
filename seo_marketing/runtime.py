"""SEO runtime composition."""

from __future__ import annotations

import os

from seo_marketing.service import SeoMarketingService
from seo_marketing.sqlite_store import SqliteSeoStore


def seo_marketing_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("SEO_MARKETING_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


class SeoMarketingRuntime:
    def __init__(self, *, service: SeoMarketingService, enabled: bool = True):
        self.service = service
        self.enabled = bool(enabled)

    def health(self) -> dict:
        return {"seo_status": "healthy" if self.enabled else "disabled", "enabled": self.enabled}

    def close(self) -> None:
        pass


def build_seo_marketing_runtime(
    *,
    env: dict | None = None,
    store=None,
    db_path: str | None = None,
    production_bundle=None,
    content_intelligence_service=None,
    product_platform_service=None,
    acquisition_service=None,
    product_media_service=None,
    observability=None,
) -> SeoMarketingRuntime | None:
    if not seo_marketing_enabled(env):
        return None
    source = env if env is not None else os.environ
    path = db_path or str(source.get("SEO_MARKETING_DB_PATH") or ":memory:")
    seo_store = store or SqliteSeoStore(path)
    from seo_marketing.analytics import AnalyticsService
    from seo_marketing.search_console import SearchConsoleService

    if production_bundle is not None:
        sc = production_bundle.search_console_provider
        analytics_svc = AnalyticsService(provider=production_bundle.analytics_provider)
        performance_provider = production_bundle.performance_provider
    else:
        from integrations.production.adapters.seo import build_seo_providers

        sc, analytics_provider, performance_provider = build_seo_providers(source)
        analytics_svc = AnalyticsService(provider=analytics_provider)
    service = SeoMarketingService(
        seo_store,
        content_intelligence_service=content_intelligence_service,
        product_platform_service=product_platform_service,
        acquisition_service=acquisition_service,
        product_media_service=product_media_service,
        observability=observability,
        search_console=SearchConsoleService(provider=sc),
        analytics=analytics_svc,
        performance_provider=performance_provider,
    )
    return SeoMarketingRuntime(service=service, enabled=True)
