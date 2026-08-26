"""Freshness evaluation helpers."""

from __future__ import annotations

from datetime import datetime, timedelta

from knowledge.models import (
    FRESHNESS_ON_DEMAND,
    FRESHNESS_STATIC,
    FRESHNESS_TTL,
    FreshnessPolicy,
)
from memory.models import utc_now


def expires_at_for(policy: FreshnessPolicy, *, now: datetime | None = None) -> datetime | None:
    stamp = now or utc_now()
    if policy.policy == FRESHNESS_STATIC:
        return None
    if policy.policy in {FRESHNESS_TTL, FRESHNESS_ON_DEMAND, "manual_refresh"}:
        ttl = policy.ttl_seconds
        if ttl is None:
            return None
        return stamp + timedelta(seconds=int(ttl))
    return stamp + timedelta(seconds=int(policy.ttl_seconds or 86400))


def is_stale(
    *,
    expires_at: datetime | None,
    policy: FreshnessPolicy,
    now: datetime | None = None,
) -> bool:
    """Stale when expires_at is in the past. Static sources use expires_at=None."""
    _ = policy  # policy shapes expires_at at ingest; honor the deadline here
    stamp = now or utc_now()
    if expires_at is None:
        return False
    exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=stamp.tzinfo)
    return exp <= stamp


def freshness_label(*, stale: bool, policy: FreshnessPolicy) -> str:
    if stale:
        return "stale"
    return policy.policy
