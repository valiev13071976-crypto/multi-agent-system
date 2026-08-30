"""Deterministic fake performance provider."""

from __future__ import annotations

from seo_marketing.errors import SEO_PROVIDER_RATE_LIMITED, SEO_PROVIDER_TIMEOUT, SeoMarketingError


class FakePerformanceProvider:
    provider_id = "fake_performance"

    def __init__(self, *, rate_limit_at: int | None = None, timeout_at: int | None = None):
        self._rate_limit_at = rate_limit_at
        self._timeout_at = timeout_at
        self._calls = 0

    def measure_url(
        self,
        *,
        tenant_id: str,
        url: str,
        measurement_type: str = "LAB",
    ) -> dict:
        self._calls += 1
        if self._timeout_at is not None and self._calls == self._timeout_at:
            raise SeoMarketingError(SEO_PROVIDER_TIMEOUT)
        if self._rate_limit_at is not None and self._calls == self._rate_limit_at:
            raise SeoMarketingError(SEO_PROVIDER_RATE_LIMITED)
        base = abs(hash(url)) % 1000 / 1000.0
        return {
            "url": url,
            "measurement_type": measurement_type,
            "LCP": round(1.5 + base, 3),
            "INP": round(100 + base * 100, 1),
            "CLS": round(base / 10, 4),
            "TTFB": round(0.2 + base / 5, 3),
        }
