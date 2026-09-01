"""Deterministic Calendar FIXTURE adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.calendar.catalog import GLOBAL_CALENDAR_STORE, CalendarStore
from integrations.calendar.errors import (
    CalendarAmbiguousTargetError,
    CalendarNotFoundError,
    CalendarTimezoneError,
    CalendarUncertainWriteOutcomeError,
    CalendarUnsupportedCapabilityError,
)
from integrations.calendar.mapping import build_preview, fingerprint_event, require_timezone, validate_attendees


@dataclass
class CalendarFixtureState(FixtureAdapterState):
    uncertain_write: bool = False
    force_ambiguous_attendee: bool = False
    tenant_override: str = ""


class CalendarFixtureAdapter(FixtureProviderAdapter):
    def __init__(self, *, state: CalendarFixtureState | None = None, store: CalendarStore | None = None):
        super().__init__("calendar", state=state or CalendarFixtureState())
        self.state: CalendarFixtureState = self.state  # type: ignore[assignment]
        self._store = store or GLOBAL_CALENDAR_STORE
        self.environment = "FIXTURE"
        self.live = False

    def verify(self, *, credential_ref: str) -> dict:
        base = super().verify(credential_ref=credential_ref)
        if not base.get("ok"):
            return base
        return {
            **base,
            "provider_identity": "fixture:calendar",
            "capabilities": ["calendar.read", "calendar.availability.read", "calendar.event.create", "calendar.event.update", "calendar.event.cancel"],
        }

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._raise_if_bad()
        params = params or {}
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(params.get("operation") or "").strip()

        if operation == "calendar_list":
            return self._store.list_calendars(tenant_id=tenant)
        if operation == "event_list":
            return self._store.list_events(
                tenant_id=tenant,
                calendar_id=str(params.get("calendar_id") or "cal-primary"),
                page=int(params.get("page") or 1),
            )
        if operation == "event_read":
            ev = self._store.get_event(
                tenant_id=tenant,
                calendar_id=str(params.get("calendar_id") or "cal-primary"),
                event_id=str(params.get("event_id") or ""),
            )
            if not ev:
                raise CalendarNotFoundError("event_not_found")
            return ev
        if operation == "free_busy":
            return self._store.free_busy(
                tenant_id=tenant,
                calendar_id=str(params.get("calendar_id") or "cal-primary"),
                start=str(params.get("start") or ""),
                end=str(params.get("end") or ""),
            )
        return self._store.list_events(tenant_id=tenant, page=int(params.get("page") or 1))

    def write(
        self,
        *,
        capability: str,
        payload: dict,
        idempotency_key: str,
        tenant_id: str = "",
        credential_ref: str = "",
    ) -> dict:
        self._raise_if_bad()
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(payload.get("operation") or "").strip()

        if idempotency_key in self.state.writes:
            cached = dict(self.state.writes[idempotency_key])
            cached["idempotent"] = True
            return cached

        if operation == "reconcile_event":
            return self._write_reconcile_event(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)

        if self.state.uncertain_write and operation not in {"reconcile_event"}:
            raise CalendarUncertainWriteOutcomeError("uncertain_write_outcome")

        if operation == "event_cancel" or capability == "calendar.event.cancel":
            return self._write_cancel(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)
        if operation == "event_update" or capability == "calendar.event.update":
            return self._write_update(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)
        if operation == "event_create" or capability == "calendar.event.create":
            return self._write_create(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)
        raise CalendarUnsupportedCapabilityError(f"unsupported_calendar_write:{capability}")

    def _write_create(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        start = str(payload.get("start") or "")
        tz_raw = payload.get("timezone")
        tz = str(tz_raw) if tz_raw is not None else ("Europe/Moscow" if ("+" in start or start.endswith("Z")) else "")
        require_timezone(timezone=tz, start=start)
        if not tz and payload.get("require_timezone"):
            raise CalendarTimezoneError("timezone_required")
        validate_attendees(
            attendees=list(payload.get("attendees") or []),
            ambiguous=self.state.force_ambiguous_attendee or bool(payload.get("ambiguous_attendees")),
        )
        if payload.get("ambiguous_recurrence"):
            raise CalendarAmbiguousTargetError("ambiguous_recurrence_scope")
        cal_id = str(payload.get("calendar_id") or "cal-primary")
        event = self._store.create_event(tenant_id=tenant, calendar_id=cal_id, payload={**payload, "timezone": tz})
        fp = fingerprint_event(
            calendar_id=cal_id,
            title=str(payload.get("title") or ""),
            start=start,
            end=str(payload.get("end") or ""),
            timezone=tz,
            attendees=list(payload.get("attendees") or []),
        )
        out = {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "event_create",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "event": event,
            "fingerprint": fp,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        cal_id = str(payload.get("calendar_id") or "cal-primary")
        event_id = str(payload.get("event_id") or "")
        patch = dict(payload.get("patch") or payload.get("fields") or {})
        if "null_field" in patch and patch["null_field"] is None:
            patch = {k: v for k, v in patch.items() if k != "null_field" or v is not None}
        event = self._store.update_event(tenant_id=tenant, calendar_id=cal_id, event_id=event_id, patch=patch)
        out = {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "event_update",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "event": event,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_cancel(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        cal_id = str(payload.get("calendar_id") or "cal-primary")
        event_id = str(payload.get("event_id") or "evt-1")
        event = self._store.cancel_event(tenant_id=tenant, calendar_id=cal_id, event_id=event_id)
        out = {
            "status": "CANCELLED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "event_cancel",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "event": event,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_reconcile_event(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        cal_id = str(payload.get("calendar_id") or "cal-primary")
        event_id = str(payload.get("event_id") or "")
        expected_title = str(payload.get("title") or "")
        event = self._store.get_event(tenant_id=tenant, calendar_id=cal_id, event_id=event_id)
        verified = "VERIFIED" if event and event.get("title") == expected_title else "UNKNOWN"
        out = {
            "status": "RECONCILED",
            "operation": "reconcile_event",
            "verified": verified,
            "observed": event,
            "mode": "FIXTURE",
            "live": False,
            "idempotent": False,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out
