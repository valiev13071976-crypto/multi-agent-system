"""Calendar mapping and validation."""

from __future__ import annotations

import hashlib

from integrations.calendar.errors import CalendarAmbiguousTargetError, CalendarTimezoneError


def build_preview(*, operation: str, before: dict | None, after: dict) -> dict:
    return {"operation": operation, "before": before or {}, "after": after, "safe": True}


def require_timezone(*, timezone: str, start: str) -> None:
    if not timezone and start and "T" in start and "+" not in start and "Z" not in start:
        raise CalendarTimezoneError("timezone_required_for_timed_event")


def validate_attendees(*, attendees: list[str], ambiguous: bool = False) -> dict:
    if ambiguous:
        raise CalendarAmbiguousTargetError("ambiguous_attendee")
    return {"count": len(attendees or [])}


def fingerprint_event(*, calendar_id: str, title: str, start: str, end: str, timezone: str, attendees: list) -> str:
    payload = "|".join([calendar_id, title, start, end, timezone, ",".join(attendees or [])])
    return hashlib.sha256(payload.encode()).hexdigest()
