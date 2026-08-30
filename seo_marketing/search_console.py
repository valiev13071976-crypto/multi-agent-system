"""Search Console integration layer (12.5)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from seo_marketing.access import SeoAccessPolicy
from seo_marketing.errors import SEO_PROVIDER_RATE_LIMITED, SEO_PROVIDER_TIMEOUT, SeoMarketingError
from seo_marketing.platform_models import SearchConsoleSnapshot
from seo_marketing.providers.fake_search_console import FakeSearchConsoleProvider

MAX_RETRIES = 3


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idempotency_key(tenant_id: str, property_id: str, date_start: str, date_end: str, token: str) -> str:
    raw = f"{tenant_id}:{property_id}:{date_start}:{date_end}:{token}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class SearchConsoleService:
    def __init__(self, provider=None, access: SeoAccessPolicy | None = None):
        self.provider = provider or FakeSearchConsoleProvider()
        self.access = access or SeoAccessPolicy()
        self._ingested: set[str] = set()

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
    ) -> SearchConsoleSnapshot:
        self.access.require_property(tenant_id=tenant_id, site_property=property_id, bound_property=bound_property)
        key = _idempotency_key(tenant_id, property_id, date_start, date_end, page_token)
        if key in self._ingested:
            return SearchConsoleSnapshot(
                snapshot_id=key,
                tenant_id=tenant_id,
                site_id=site_id,
                property_id=property_id,
                date_start=date_start,
                date_end=date_end,
                rows=(),
                retrieved_at=_utc(),
                freshness="idempotent_replay",
            )
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                result = self.provider.get_query_performance(
                    tenant_id=tenant_id,
                    property_id=property_id,
                    date_start=date_start,
                    date_end=date_end,
                    page_token=page_token,
                )
                snap = SearchConsoleSnapshot(
                    snapshot_id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    site_id=site_id,
                    property_id=property_id,
                    date_start=date_start,
                    date_end=date_end,
                    rows=tuple(result.get("rows") or []),
                    retrieved_at=_utc(),
                    freshness=str(result.get("freshness") or "delayed"),
                )
                self._ingested.add(key)
                return snap
            except SeoMarketingError as exc:
                last_exc = exc
                if exc.code not in {SEO_PROVIDER_RATE_LIMITED, SEO_PROVIDER_TIMEOUT}:
                    raise
        raise last_exc  # type: ignore[misc]
