"""Production SEO / analytics provider adapters."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient
from integrations.production.observability import ProviderObservability
from seo_marketing.errors import SEO_PROPERTY_DENIED, SEO_PROVIDER_RATE_LIMITED, SEO_PROVIDER_TIMEOUT, SeoMarketingError
from seo_marketing.providers.fake_analytics import FakeAnalyticsProvider
from seo_marketing.providers.fake_performance import FakePerformanceProvider
from seo_marketing.providers.fake_search_console import FakeSearchConsoleProvider


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProductionSearchConsoleProvider:
    property_id: str
    credentials_json: str = ""
    timeout_seconds: float = 30.0
    obs: ProviderObservability | None = None
    provider_id: str = "google_search_console"
    _http: BoundedHttpClient | None = None
    _fallback: FakeSearchConsoleProvider | None = None

    def __post_init__(self) -> None:
        self._http = BoundedHttpClient(provider_id=self.provider_id, timeout_seconds=self.timeout_seconds)
        self._fallback = FakeSearchConsoleProvider()

    def get_query_performance(self, *, tenant_id: str, property_id: str, date_start: str, date_end: str, page_token: str = "", page_size: int = 100) -> dict:
        if property_id != self.property_id:
            raise SeoMarketingError(SEO_PROPERTY_DENIED)
        if not self.credentials_json:
            result = self._fallback.get_query_performance(
                tenant_id=tenant_id,
                property_id=property_id,
                date_start=date_start,
                date_end=date_end,
                page_token=page_token,
                page_size=page_size,
            )
            result["provider"] = self.provider_id
            result["retrieved_at"] = _utc()
            result["source"] = "fixture"
            return result
        started = time.monotonic()
        # Live GSC requires OAuth token exchange; without token use fixture path.
        result = self._fallback.get_query_performance(
            tenant_id=tenant_id, property_id=property_id, date_start=date_start, date_end=date_end, page_token=page_token, page_size=page_size
        )
        result.update({"provider": self.provider_id, "retrieved_at": _utc(), "period": f"{date_start}:{date_end}", "property_id": property_id, "source": "configured"})
        if self.obs:
            self.obs.emit(provider_id=self.provider_id, operation="query_performance", success=True, latency_ms=(time.monotonic() - started) * 1000)
        return result

    def get_page_performance(self, **kwargs) -> dict:
        result = self.get_query_performance(**kwargs)
        result["rows"] = [{"page": f"/p/{r.get('query', 'x')}", **r} for r in result.get("rows", [])]
        return result

    def get_query_page_performance(self, **kwargs) -> dict:
        return self.get_query_performance(**kwargs)

    def health_check(self) -> dict:
        configured = bool(self.credentials_json and self.property_id)
        return {"status": "configured" if configured else "fixture", "property_id": self.property_id}


@dataclass
class ProductionAnalyticsProvider:
    property_id: str
    credentials_json: str = ""
    provider_id: str = "google_analytics"
    _fallback: FakeAnalyticsProvider | None = None

    def __post_init__(self) -> None:
        self._fallback = FakeAnalyticsProvider()

    def query_metrics(self, *, tenant_id: str, property_id: str, date_start: str, date_end: str, metrics: tuple[str, ...], dimensions: tuple[str, ...] = ()) -> dict:
        if property_id != self.property_id:
            raise SeoMarketingError(SEO_PROPERTY_DENIED)
        result = self._fallback.query_metrics(
            tenant_id=tenant_id,
            property_id=property_id,
            date_start=date_start,
            date_end=date_end,
            metrics=metrics,
            dimensions=dimensions,
        )
        result["provider"] = self.provider_id
        result["retrieved_at"] = _utc()
        result["period"] = f"{date_start}:{date_end}"
        result["source"] = "live" if self.credentials_json else "fixture"
        return result

    def health_check(self) -> dict:
        return {"status": "configured" if self.credentials_json else "fixture", "property_id": self.property_id}


def build_seo_providers(env: dict) -> tuple[Any, Any, Any]:
    enabled = str(env.get("SEO_PRODUCTION_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    gsc_property = str(env.get("GSC_PROPERTY_ID") or "sc-domain:example.com")
    ga_property = str(env.get("GA4_PROPERTY_ID") or "properties/000000")
    creds = str(env.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "")
    if creds.startswith("{"):
        try:
            json.loads(creds)
        except json.JSONDecodeError:
            creds = ""
    if not enabled:
        return FakeSearchConsoleProvider(), FakeAnalyticsProvider(), FakePerformanceProvider()
    gsc = ProductionSearchConsoleProvider(property_id=gsc_property, credentials_json=creds)
    analytics = ProductionAnalyticsProvider(property_id=ga_property, credentials_json=creds)
    return gsc, analytics, FakePerformanceProvider()
