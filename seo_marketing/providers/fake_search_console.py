"""Deterministic fake Search Console provider for closure tests."""

from __future__ import annotations

from seo_marketing.errors import SEO_PROPERTY_DENIED, SEO_PROVIDER_RATE_LIMITED, SEO_PROVIDER_TIMEOUT, SeoMarketingError


class FakeSearchConsoleProvider:
    provider_id = "fake_search_console"

    def __init__(self, *, rate_limit_at: int | None = None, timeout_at: int | None = None):
        self._rate_limit_at = rate_limit_at
        self._timeout_at = timeout_at
        self._calls = 0

    def get_query_performance(
        self,
        *,
        tenant_id: str,
        property_id: str,
        date_start: str,
        date_end: str,
        page_token: str = "",
        page_size: int = 100,
    ) -> dict:
        self._calls += 1
        if self._timeout_at is not None and self._calls == self._timeout_at:
            raise SeoMarketingError(SEO_PROVIDER_TIMEOUT)
        if self._rate_limit_at is not None and self._calls == self._rate_limit_at:
            raise SeoMarketingError(SEO_PROVIDER_RATE_LIMITED)
        if property_id.endswith("-foreign"):
            raise SeoMarketingError(SEO_PROPERTY_DENIED)
        start = int(page_token or "0")
        total = 500
        rows = []
        for i in range(start, min(start + page_size, total)):
            rows.append(
                {
                    "query": f"keyword-{i}",
                    "clicks": i % 17,
                    "impressions": 100 + i,
                    "ctr": round((i % 17) / max(100 + i, 1), 4),
                    "position": 5 + (i % 10),
                }
            )
        next_token = str(start + page_size) if start + page_size < total else ""
        return {
            "rows": rows,
            "next_page_token": next_token,
            "date_start": date_start,
            "date_end": date_end,
            "property_id": property_id,
            "freshness": "delayed_48h",
        }

    def get_page_performance(self, **kwargs) -> dict:
        result = self.get_query_performance(**kwargs)
        result["rows"] = [{"page": f"/p/{r['query']}", **r} for r in result["rows"]]
        return result

    def get_query_page_performance(self, **kwargs) -> dict:
        result = self.get_query_performance(**kwargs)
        result["rows"] = [
            {"query": r["query"], "page": f"/p/{r['query']}", **{k: r[k] for k in ("clicks", "impressions", "ctr", "position")}}
            for r in result["rows"]
        ]
        return result
