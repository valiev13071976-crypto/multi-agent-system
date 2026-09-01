"""1C inbound event readiness — verify, dedupe, no direct WRITE."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class OneCWebhookEvent:
    event_id: str
    tenant_id: str
    event_type: str
    payload_summary: dict
    verified: bool
    dedupe_key: str


class OneCWebhookReadiness:
    def __init__(self):
        self._seen: set[str] = set()

    def verify_token(self, *, body: bytes, secret: str, token: str) -> bool:
        if not secret or not token:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, token)

    def normalize(self, *, tenant_id: str, raw: dict, verified: bool) -> OneCWebhookEvent:
        event_type = str(raw.get("event") or raw.get("Event") or "unknown")
        entity_id = str(raw.get("object_id") or raw.get("Ref") or "")
        dedupe = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        duplicate = dedupe in self._seen
        if not duplicate:
            self._seen.add(dedupe)
        return OneCWebhookEvent(
            event_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            event_type=event_type,
            payload_summary={"entity_id": entity_id, "duplicate": duplicate},
            verified=verified,
            dedupe_key=dedupe,
        )

    def to_canonical_event(self, event: OneCWebhookEvent) -> dict:
        return {
            "source": "1c",
            "tenant_id": event.tenant_id,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "verified": event.verified,
            "dedupe_key": event.dedupe_key,
            "policy": "NO_DIRECT_WRITE",
            "route": "canonical_event_workflow",
        }
