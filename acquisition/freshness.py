"""Freshness evaluation for acquired observations."""

from __future__ import annotations

from datetime import datetime, timedelta

from acquisition.models import (
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    FreshnessPolicy,
    utc_now,
)


def freshness_label(
    *,
    fetched_at: datetime | None,
    source_updated_at: datetime | None = None,
    policy: FreshnessPolicy | None = None,
    now: datetime | None = None,
) -> str:
    stamp = now or utc_now()
    policy = policy or FreshnessPolicy()
    reference = source_updated_at or fetched_at
    if reference is None:
        return FRESHNESS_UNKNOWN if policy.unknown_if_missing_timestamp else FRESHNESS_STALE
    if policy.stale_after_seconds is None:
        return FRESHNESS_FRESH
    ref = reference if reference.tzinfo else reference.replace(tzinfo=stamp.tzinfo)
    age = stamp - ref
    if age > timedelta(seconds=int(policy.stale_after_seconds)):
        return FRESHNESS_STALE
    return FRESHNESS_FRESH


def is_stale(
    *,
    fetched_at: datetime | None,
    source_updated_at: datetime | None = None,
    policy: FreshnessPolicy | None = None,
    now: datetime | None = None,
) -> bool:
    return freshness_label(
        fetched_at=fetched_at,
        source_updated_at=source_updated_at,
        policy=policy,
        now=now,
    ) == FRESHNESS_STALE
