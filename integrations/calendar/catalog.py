"""Tenant-scoped calendar fixture store."""

from __future__ import annotations

import copy
import uuid


def _seed() -> dict[str, dict]:
    base = {
        "cal-primary": {
            "calendar_id": "cal-primary",
            "name": "Primary",
            "timezone": "Europe/Moscow",
            "events": {
                "evt-1": {
                    "event_id": "evt-1",
                    "calendar_id": "cal-primary",
                    "title": "Supplier sync",
                    "description": "Quarterly review",
                    "location": "Online",
                    "start": "2026-02-01T10:00:00+03:00",
                    "end": "2026-02-01T11:00:00+03:00",
                    "timezone": "Europe/Moscow",
                    "all_day": False,
                    "organizer": "mb-a@fixture.local",
                    "attendees": ["supplier@example.com"],
                    "status": "CONFIRMED",
                    "recurrence": None,
                }
            },
        }
    }
    tenant_b = copy.deepcopy(base)
    tenant_b["cal-primary"]["calendar_id"] = "cal-b-primary"
    tenant_b["cal-primary"]["events"]["evt-1"]["event_id"] = "evt-b-1"
    tenant_b["cal-primary"]["events"]["evt-1"]["calendar_id"] = "cal-b-primary"
    return {"tenant-a": copy.deepcopy(base), "tenant-b": tenant_b, "default": copy.deepcopy(base)}


def normalize_event(raw: dict) -> dict:
    return {
        "event_id": raw.get("event_id"),
        "calendar_id": raw.get("calendar_id"),
        "title": raw.get("title") or "",
        "description": raw.get("description") or "",
        "location": raw.get("location") or "",
        "start": raw.get("start") or "",
        "end": raw.get("end") or "",
        "timezone": raw.get("timezone") or "",
        "all_day": bool(raw.get("all_day")),
        "organizer": raw.get("organizer") or "",
        "attendees": list(raw.get("attendees") or []),
        "status": raw.get("status") or "",
        "recurrence": raw.get("recurrence"),
        "mode": "FIXTURE",
        "live": False,
    }


class CalendarStore:
    def __init__(self):
        self._calendars = _seed()
        self._write_counts: dict[str, int] = {}

    def calendars(self, tenant_id: str) -> dict[str, dict]:
        tid = tenant_id or "default"
        if tid not in self._calendars:
            self._calendars[tid] = copy.deepcopy(self._calendars["default"])
        return self._calendars[tid]

    def list_calendars(self, *, tenant_id: str) -> dict:
        cals = self.calendars(tenant_id)
        return {
            "items": [{"calendar_id": c["calendar_id"], "name": c["name"], "timezone": c["timezone"], "mode": "FIXTURE", "live": False} for c in cals.values()],
            "mode": "FIXTURE",
            "live": False,
        }

    def list_events(self, *, tenant_id: str, calendar_id: str = "cal-primary", page: int = 1, page_size: int = 5) -> dict:
        cal = self.calendars(tenant_id).get(calendar_id) or list(self.calendars(tenant_id).values())[0]
        items = [normalize_event(e) for e in (cal.get("events") or {}).values()]
        start = (page - 1) * page_size
        chunk = items[start : start + page_size]
        next_page = page + 1 if start + page_size < len(items) else None
        return {"items": chunk, "page": page, "next_page": next_page, "bounded": True, "mode": "FIXTURE", "live": False}

    def get_event(self, *, tenant_id: str, calendar_id: str, event_id: str) -> dict | None:
        cal = self.calendars(tenant_id).get(calendar_id)
        if not cal:
            return None
        raw = (cal.get("events") or {}).get(event_id)
        return normalize_event(raw) if raw else None

    def free_busy(self, *, tenant_id: str, calendar_id: str, start: str, end: str) -> dict:
        return {
            "calendar_id": calendar_id,
            "start": start,
            "end": end,
            "busy": [{"start": "2026-02-01T10:00:00+03:00", "end": "2026-02-01T11:00:00+03:00"}],
            "mode": "FIXTURE",
            "live": False,
        }

    def create_event(self, *, tenant_id: str, calendar_id: str, payload: dict) -> dict:
        cal = self.calendars(tenant_id).get(calendar_id) or list(self.calendars(tenant_id).values())[0]
        eid = f"evt-{uuid.uuid4().hex[:6]}"
        event = normalize_event(
            {
                "event_id": eid,
                "calendar_id": cal["calendar_id"],
                "title": payload.get("title"),
                "description": payload.get("description"),
                "location": payload.get("location"),
                "start": payload.get("start"),
                "end": payload.get("end"),
                "timezone": payload.get("timezone"),
                "all_day": payload.get("all_day", False),
                "organizer": payload.get("organizer") or "mb-a@fixture.local",
                "attendees": list(payload.get("attendees") or []),
                "status": "CONFIRMED",
                "recurrence": payload.get("recurrence"),
            }
        )
        cal.setdefault("events", {})[eid] = event
        return event

    def update_event(self, *, tenant_id: str, calendar_id: str, event_id: str, patch: dict) -> dict:
        cal = self.calendars(tenant_id).get(calendar_id)
        if not cal:
            return {}
        events = cal.setdefault("events", {})
        raw = dict(events.get(event_id) or {})
        for k, v in patch.items():
            if v is not None:
                raw[k] = v
        events[event_id] = raw
        return normalize_event(raw)

    def cancel_event(self, *, tenant_id: str, calendar_id: str, event_id: str) -> dict:
        ev = self.update_event(tenant_id=tenant_id, calendar_id=calendar_id, event_id=event_id, patch={"status": "CANCELLED"})
        return ev

    def record_write(self, key: str) -> int:
        self._write_counts[key] = self._write_counts.get(key, 0) + 1
        return self._write_counts[key]

    def write_count(self, key: str) -> int:
        return self._write_counts.get(key, 0)


GLOBAL_CALENDAR_STORE = CalendarStore()
