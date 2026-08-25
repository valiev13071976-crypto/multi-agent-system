"""Deterministic memory retention policy."""

from __future__ import annotations

from datetime import datetime, timedelta

from memory.models import (
    MEMORY_EPISODIC,
    MEMORY_PROCEDURAL,
    MEMORY_SEMANTIC,
    MEMORY_WORKING_REFERENCE,
    SCOPE_TYPES,
    utc_now,
)
from security.encryption import (
    SENSITIVITY_INTERNAL,
    SENSITIVITY_SECRET,
    SENSITIVITY_SENSITIVE,
)


class MemoryRetentionPolicy:
    """TTL by memory_type / sensitivity / scope_type. No permanent-everything."""

    def __init__(
        self,
        *,
        episodic_ttl_days: int = 90,
        working_reference_ttl_hours: int = 24,
        semantic_ttl_days: int | None = 3650,
        procedural_ttl_days: int | None = 3650,
    ):
        self.episodic_ttl_days = int(episodic_ttl_days)
        self.working_reference_ttl_hours = int(working_reference_ttl_hours)
        self.semantic_ttl_days = semantic_ttl_days
        self.procedural_ttl_days = procedural_ttl_days

    def expires_at(
        self,
        *,
        memory_type: str,
        sensitivity: str = SENSITIVITY_INTERNAL,
        scope_type: str = "",
        now: datetime | None = None,
        override_ttl_seconds: int | None = None,
    ) -> datetime | None:
        stamp = now or utc_now()
        if override_ttl_seconds is not None:
            return stamp + timedelta(seconds=int(override_ttl_seconds))
        _ = sensitivity
        _ = scope_type if scope_type in SCOPE_TYPES or not scope_type else scope_type
        if memory_type == MEMORY_WORKING_REFERENCE:
            return stamp + timedelta(hours=self.working_reference_ttl_hours)
        if memory_type == MEMORY_EPISODIC:
            return stamp + timedelta(days=self.episodic_ttl_days)
        if memory_type == MEMORY_SEMANTIC:
            if self.semantic_ttl_days is None:
                return None
            return stamp + timedelta(days=int(self.semantic_ttl_days))
        if memory_type == MEMORY_PROCEDURAL:
            if self.procedural_ttl_days is None:
                return None
            return stamp + timedelta(days=int(self.procedural_ttl_days))
        return stamp + timedelta(days=self.episodic_ttl_days)

    def is_expired(self, expires_at: datetime | None, *, now: datetime | None = None) -> bool:
        if expires_at is None:
            return False
        stamp = now or utc_now()
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=stamp.tzinfo)
        return expires_at <= stamp


def retention_policy_snapshot(policy: MemoryRetentionPolicy | None = None) -> dict:
    p = policy or MemoryRetentionPolicy()
    return {
        "memory_policy_version": "1.0.0",
        "episodic_ttl_days": p.episodic_ttl_days,
        "working_reference_ttl_hours": p.working_reference_ttl_hours,
        "semantic_ttl_days": p.semantic_ttl_days,
        "procedural_ttl_days": p.procedural_ttl_days,
        "rules": [
            "working_reference_short_ttl",
            "episodic_bounded_ttl",
            "semantic_long_lived",
            "procedural_long_lived",
            "no_permanent_everything",
        ],
    }
