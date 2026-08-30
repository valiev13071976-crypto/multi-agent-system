"""Competitor and trend analysis — evidence-based."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from content_intel.platform_models import INFERRED, OBSERVED, CompetitorProfile, TrendSignal, STATUS_STALE


def build_competitor_profile(
    *,
    tenant_id: str,
    name: str,
    category: str,
    observations: list[dict],
    evidence_refs: tuple[str, ...],
) -> CompetitorProfile:
    obs = []
    for row in observations:
        kind = str(row.get("kind") or OBSERVED)
        if kind not in {OBSERVED, INFERRED}:
            kind = OBSERVED
        obs.append({"field": row.get("field"), "value": row.get("value"), "kind": kind})
    return CompetitorProfile(
        competitor_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=name,
        category=category,
        observations=tuple(obs),
        evidence_refs=evidence_refs,
        observation_kind=OBSERVED,
    )


def compute_trend_velocity(counts: list[float], *, window_hours: float) -> float:
    if len(counts) < 2 or window_hours <= 0:
        return 0.0
    return (counts[-1] - counts[0]) / window_hours


def build_trend_signal(
    *,
    tenant_id: str,
    topic: str,
    counts: list[float],
    evidence_count: int,
    window_hours: float = 24.0,
    stale_after_hours: float = 72.0,
    last_observed: datetime | None = None,
) -> TrendSignal:
    now = datetime.now(timezone.utc)
    last = last_observed or now
    magnitude = counts[-1] if counts else 0.0
    velocity = compute_trend_velocity(counts, window_hours=window_hours)
    stale = (now - last).total_seconds() / 3600.0 > stale_after_hours
    confidence = "high" if evidence_count >= 3 else "low"
    return TrendSignal(
        trend_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        topic=topic,
        magnitude=float(magnitude),
        velocity=float(velocity),
        first_observed=now,
        last_observed=last,
        evidence_count=int(evidence_count),
        status=STATUS_STALE if stale else "current",
        confidence_label=confidence,
    )
