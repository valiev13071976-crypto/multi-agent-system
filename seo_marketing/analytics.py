"""Analytics intelligence (12.6)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from seo_marketing.access import SeoAccessPolicy
from seo_marketing.errors import SeoMarketingError
from seo_marketing.platform_models import AnalyticsSnapshot
from seo_marketing.providers.fake_analytics import FakeAnalyticsProvider

MAX_RETRIES = 3


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_ctr(clicks: int, impressions: int) -> Decimal:
    if impressions <= 0:
        return Decimal("0")
    return (Decimal(clicks) / Decimal(impressions)).quantize(Decimal("0.0001"))


def compute_conversion_rate(conversions: int, sessions: int) -> Decimal:
    if sessions <= 0:
        return Decimal("0")
    return (Decimal(conversions) / Decimal(sessions)).quantize(Decimal("0.0001"))


def compute_delta(current: Decimal, prior: Decimal) -> Decimal:
    return (current - prior).quantize(Decimal("0.0001"))


def windows_compatible(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start == b_start and a_end == b_end


class AnalyticsService:
    def __init__(self, provider=None, access: SeoAccessPolicy | None = None):
        self.provider = provider or FakeAnalyticsProvider()
        self.access = access or SeoAccessPolicy()

    def ingest(
        self,
        *,
        tenant_id: str,
        site_id: str,
        bound_property: str,
        property_id: str,
        date_start: str,
        date_end: str,
        page_token: str = "",
    ) -> AnalyticsSnapshot:
        self.access.require_analytics_property(tenant_id=tenant_id, requested=property_id, bound=bound_property)
        result = self.provider.query_metrics(
            tenant_id=tenant_id,
            property_id=property_id,
            date_start=date_start,
            date_end=date_end,
            page_token=page_token,
        )
        return AnalyticsSnapshot(
            snapshot_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            site_id=site_id,
            property_id=property_id,
            date_start=date_start,
            date_end=date_end,
            rows=tuple(result.get("rows") or []),
            retrieved_at=_utc(),
        )

    def join_with_search_console(
        self,
        *,
        analytics: AnalyticsSnapshot,
        search_console: AnalyticsSnapshot | object,
        sc_window_start: str,
        sc_window_end: str,
    ) -> list[dict]:
        if not windows_compatible(analytics.date_start, analytics.date_end, sc_window_start, sc_window_end):
            raise SeoMarketingError("SEO_CONFLICT", "incompatible analytics/search console windows")
        return [{"landing_page": r.get("landing_page"), "sessions": r.get("sessions")} for r in analytics.rows]
