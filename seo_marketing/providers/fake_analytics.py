"""Deterministic fake analytics provider for closure tests."""

from __future__ import annotations

from decimal import Decimal

from seo_marketing.errors import SEO_ANALYTICS_PROPERTY_DENIED, SEO_PROVIDER_RATE_LIMITED, SeoMarketingError


class FakeAnalyticsProvider:
    provider_id = "fake_analytics"

    def __init__(self, *, rate_limit_at: int | None = None):
        self._rate_limit_at = rate_limit_at
        self._calls = 0

    def query_metrics(
        self,
        *,
        tenant_id: str,
        property_id: str,
        date_start: str,
        date_end: str,
        dimensions: tuple[str, ...] = ("landing_page",),
        page_token: str = "",
        page_size: int = 100,
    ) -> dict:
        self._calls += 1
        if self._rate_limit_at is not None and self._calls == self._rate_limit_at:
            raise SeoMarketingError(SEO_PROVIDER_RATE_LIMITED)
        if property_id.endswith("-foreign"):
            raise SeoMarketingError(SEO_ANALYTICS_PROPERTY_DENIED)
        start = int(page_token or "0")
        rows = []
        for i in range(start, min(start + page_size, start + 50)):
            row = {
                "sessions": 100 + i,
                "landing_page": f"/landing/{i}",
                "conversions": i % 5,
            }
            if "revenue" in dimensions or True:
                row["revenue"] = str(Decimal("10.50") * Decimal(i + 1))
            rows.append(row)
        next_token = str(start + page_size) if len(rows) == page_size else ""
        return {
            "rows": rows,
            "next_page_token": next_token,
            "date_start": date_start,
            "date_end": date_end,
            "property_id": property_id,
        }
