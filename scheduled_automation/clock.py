"""Injectable clock for deterministic scheduling tests."""

from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    def __init__(self, *, now: datetime | None = None):
        self._now = now or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("naive_datetime_not_allowed")
        self._now = value

    def advance_seconds(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)
