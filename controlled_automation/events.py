"""Business event envelope and idempotent ingestion."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from controlled_automation.models import BusinessEvent


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BusinessEventStore:
    def __init__(self):
        self._events: dict[tuple[str, str], BusinessEvent] = {}
        self._seen: set[str] = set()

    def ingest(self, *, tenant_id: str, payload: dict[str, Any], chain_depth: int = 0, max_depth: int = 5) -> BusinessEvent:
        if chain_depth > max_depth:
            raise ValueError("chain_depth_exceeded")
        eid = str(payload.get("event_id") or uuid.uuid4())
        dedupe_key = f"{tenant_id}:{eid}"
        if dedupe_key in self._seen:
            return self._events[(tenant_id, eid)]
        origin = payload.get("origin_automation_id")
        causation = payload.get("causation_id") or payload.get("trace_id") or eid
        if origin and causation == origin:
            raise ValueError("self_trigger_loop")
        ev = BusinessEvent(
            event_id=eid,
            event_type=str(payload.get("event_type") or "generic"),
            tenant_id=tenant_id,
            owner_id=str(payload.get("owner_id") or "system"),
            occurred_at=str(payload.get("occurred_at") or utc_iso()),
            source=str(payload.get("source") or "internal"),
            subject_type=str(payload.get("subject_type") or "unknown"),
            subject_id=str(payload.get("subject_id") or ""),
            payload_ref=str(payload.get("payload_ref") or ""),
            trace_id=str(payload.get("trace_id") or uuid.uuid4()),
            schema_version=str(payload.get("schema_version") or "1"),
            origin_automation_id=origin,
            causation_id=causation,
        )
        self._events[(tenant_id, eid)] = ev
        self._seen.add(dedupe_key)
        return ev

    def get(self, *, tenant_id: str, event_id: str) -> BusinessEvent | None:
        return self._events.get((tenant_id, event_id))
