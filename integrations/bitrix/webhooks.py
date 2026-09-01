"""Bitrix inbound webhook readiness — verify, normalize, no uncontrolled WRITE."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class BitrixWebhookEvent:
    event_id: str
    tenant_id: str
    event_type: str
    payload_summary: dict
    verified: bool
    dedupe_key: str


class BitrixWebhookReadiness:
    """Safe contract for inbound Bitrix events — readiness only, not live activation required."""

    def __init__(self):
        self._seen: set[str] = set()

    def verify_signature(self, *, body: bytes, secret: str, signature: str) -> bool:
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def normalize(
        self,
        *,
        tenant_id: str,
        raw: dict,
        verified: bool,
    ) -> BitrixWebhookEvent:
        event_type = str(raw.get("event") or raw.get("EVENT") or "unknown")
        entity_id = str(raw.get("data", {}).get("FIELDS", {}).get("ID") or raw.get("ID") or "")
        dedupe = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        if dedupe in self._seen:
            return BitrixWebhookEvent(
                event_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                event_type=event_type,
                payload_summary={"entity_id": entity_id, "duplicate": True},
                verified=verified,
                dedupe_key=dedupe,
            )
        self._seen.add(dedupe)
        return BitrixWebhookEvent(
            event_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            event_type=event_type,
            payload_summary={"entity_id": entity_id, "duplicate": False},
            verified=verified,
            dedupe_key=dedupe,
        )

    def to_canonical_event(self, event: BitrixWebhookEvent) -> dict:
        return {
            "source": "bitrix",
            "tenant_id": event.tenant_id,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "verified": event.verified,
            "dedupe_key": event.dedupe_key,
            "policy": "NO_DIRECT_WRITE",
            "route": "canonical_event_workflow",
        }
