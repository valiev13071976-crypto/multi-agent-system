"""Recurrence and next-run computation with timezone awareness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scheduled_automation.config import MIN_INTERVAL_SECONDS
from scheduled_automation.errors import INVALID_RECURRENCE, INVALID_TIMEZONE, INVALID_SCHEDULE, ScheduledAutomationError
from scheduled_automation.models import (
    MISFIRE_CATCH_UP_BOUNDED,
    MISFIRE_RUN_ONCE,
    MISFIRE_SKIP,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONCE,
    SCHEDULE_WEEKLY,
)


def parse_utc(value: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ScheduledAutomationError(INVALID_SCHEDULE, "naive_datetime")
    return dt.astimezone(timezone.utc)


def validate_timezone(tz: str):
    name = str(tz or "").strip()
    if name in {"UTC", "Etc/UTC", "GMT", "Zulu"}:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduledAutomationError(INVALID_TIMEZONE, str(tz)) from exc


def compute_next_run(
    *,
    schedule_type: str,
    timezone_name: str,
    start_at: str,
    end_at: str | None,
    interval_seconds: int | None,
    daily_time: str | None,
    weekly_day: int | None,
    from_time: datetime,
    occurrence_count: int,
    max_occurrences: int | None,
) -> datetime | None:
    if max_occurrences is not None and occurrence_count >= max_occurrences:
        return None
    start = parse_utc(start_at)
    if from_time < start:
        from_time = start
    if end_at:
        end = parse_utc(end_at)
        if from_time > end:
            return None

    if schedule_type == SCHEDULE_ONCE:
        return start if occurrence_count == 0 and from_time <= start else None

    if schedule_type == SCHEDULE_INTERVAL:
        if not interval_seconds or interval_seconds < MIN_INTERVAL_SECONDS:
            raise ScheduledAutomationError(INVALID_RECURRENCE, "interval_too_small")
        base = start if occurrence_count == 0 else from_time
        return base if occurrence_count == 0 else base + timedelta(seconds=int(interval_seconds))

    tz = validate_timezone(timezone_name)
    local = from_time.astimezone(tz)
    if schedule_type == SCHEDULE_DAILY:
        if not daily_time:
            raise ScheduledAutomationError(INVALID_RECURRENCE, "daily_time_required")
        hour, minute = [int(x) for x in daily_time.split(":", 1)]
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    if schedule_type == SCHEDULE_WEEKLY:
        if weekly_day is None or weekly_day < 0 or weekly_day > 6:
            raise ScheduledAutomationError(INVALID_RECURRENCE, "weekly_day_invalid")
        if not daily_time:
            raise ScheduledAutomationError(INVALID_RECURRENCE, "daily_time_required")
        hour, minute = [int(x) for x in daily_time.split(":", 1)]
        days_ahead = (int(weekly_day) - local.weekday()) % 7
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
        if candidate <= local:
            candidate = candidate + timedelta(days=7)
        return candidate.astimezone(timezone.utc)

    raise ScheduledAutomationError(INVALID_RECURRENCE, schedule_type)


def misfire_occurrences(
    *,
    policy: str,
    due_at: datetime,
    now: datetime,
    max_catch_up: int,
) -> list[datetime]:
    if now <= due_at:
        return [due_at]
    if policy == MISFIRE_SKIP:
        return []
    if policy == MISFIRE_RUN_ONCE:
        return [now]
    if policy == MISFIRE_CATCH_UP_BOUNDED:
        return [now]
    return []


def occurrence_id(schedule_id: str, version: int, scheduled_for: datetime) -> str:
    return f"{schedule_id}:v{version}:{scheduled_for.astimezone(timezone.utc).isoformat()}"


def execution_key(schedule_id: str, version: int, scheduled_for: datetime) -> str:
    return f"schedule-occurrence:{schedule_id}:{version}:{int(scheduled_for.timestamp())}"
