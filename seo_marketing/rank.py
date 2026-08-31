"""Rank / SERP observation contracts — provider-neutral, no invented ranks."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from seo_marketing.errors import SEO_RANK_UNAVAILABLE, SeoMarketingError
from seo_marketing.platform_models import (
    NOT_AVAILABLE,
    OBSERVED_RANK,
    RANK_DECLINED,
    RANK_DROPPED,
    RANK_GAINED,
    RANK_IMPROVED,
    RANK_LOST,
    RANK_NEW,
    RANK_PROFILE_VERSION,
    RANK_UNCHANGED,
    RANK_UNKNOWN,
    RankObservation,
    SERPObservation,
    TRUSTED_EXTERNAL,
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def ingest_rank_observation(
    *,
    tenant_id: str,
    site_id: str,
    keyword: str,
    page_url: str,
    position: float | None,
    search_engine: str = "google",
    country: str = "US",
    device: str = "desktop",
    provider: str = "fixture",
    observed_at: str = "",
) -> RankObservation:
    if position is None:
        status = NOT_AVAILABLE
        trust = NOT_AVAILABLE
    else:
        status = OBSERVED_RANK
        trust = TRUSTED_EXTERNAL
    _ = RANK_PROFILE_VERSION
    return RankObservation(
        observation_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        site_id=site_id,
        keyword=keyword,
        page_url=page_url,
        position=position,
        search_engine=search_engine,
        country=country,
        device=device,
        observed_at=observed_at or _utc(),
        provider=provider,
        status=status,
        trust_level=trust,
    )


def classify_rank_change(prior: float | None, current: float | None) -> str:
    if prior is None and current is None:
        return RANK_UNKNOWN
    if prior is None and current is not None:
        return RANK_NEW
    if prior is not None and current is None:
        return RANK_DROPPED
    if prior == current:
        return RANK_UNCHANGED
    # Lower position number = better
    assert prior is not None and current is not None
    if current < prior:
        return RANK_IMPROVED if prior - current < 10 else RANK_GAINED
    return RANK_DECLINED if current - prior < 10 else RANK_LOST


def compare_rank_history(history: list[RankObservation]) -> list[dict]:
    """Never overwrite history — return deltas between consecutive observations."""
    by_key: dict[tuple[str, str], list[RankObservation]] = {}
    for obs in sorted(history, key=lambda o: o.observed_at):
        by_key.setdefault((obs.keyword, obs.page_url), []).append(obs)
    out: list[dict] = []
    for (kw, url), series in by_key.items():
        for i in range(1, len(series)):
            prev, cur = series[i - 1], series[i]
            out.append(
                {
                    "keyword": kw,
                    "page_url": url,
                    "prior_position": prev.position,
                    "current_position": cur.position,
                    "change": classify_rank_change(prev.position, cur.position),
                    "prior_id": prev.observation_id,
                    "current_id": cur.observation_id,
                }
            )
    return out


def ingest_serp_observation(
    *,
    tenant_id: str,
    query: str,
    results: list[dict],
    country: str = "US",
    language: str = "en",
    search_engine: str = "google",
    provider: str = "fixture",
) -> SERPObservation:
    # Do not invent results
    if results is None:
        raise SeoMarketingError(SEO_RANK_UNAVAILABLE)
    return SERPObservation(
        observation_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        query=query,
        country=country,
        language=language,
        search_engine=search_engine,
        observed_at=_utc(),
        results=tuple(results),
        provider=provider,
        trust_level=TRUSTED_EXTERNAL,
    )


class FakeRankProvider:
    """Deterministic fixture — does not claim live search scraping."""

    provider_id = "fake-rank"

    def check(self, *, keyword: str, page_url: str) -> dict:
        # Stable fake position from hash — fixture only
        pos = (sum(ord(c) for c in keyword) % 20) + 1
        return {"position": float(pos), "provider": self.provider_id, "fake": True}
