"""Ozon inbound event readiness — polling-first boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class OzonWebhookEvent:
    event_id: str
    tenant_id: str
    event_type: str
    payload_summary: dict
    verified: bool
    dedupe_key: str


class OzonWebhookReadiness:
    """Safe webhook boundary when configured; otherwise polling via import_status."""

    def __init__(self):
        self._seen: set[str] = set()

    def verify_signature(self, *, body: bytes, secret: str, signature: str) -> bool:
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def normalize(self, *, tenant_id: str, raw: dict, verified: bool) -> OzonWebhookEvent:
        event_type = str(raw.get("message_type") or raw.get("type") or "unknown")
        entity_id = str(raw.get("product_id") or raw.get("posting_number") or raw.get("task_id") or "")
        dedupe = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        duplicate = dedupe in self._seen
        if not duplicate:
            self._seen.add(dedupe)
        return OzonWebhookEvent(
            event_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            event_type=event_type,
            payload_summary={"entity_id": entity_id, "duplicate": duplicate},
            verified=verified,
            dedupe_key=dedupe,
        )

    def to_canonical_event(self, event: OzonWebhookEvent) -> dict:
        return {
            "source": "ozon",
            "tenant_id": event.tenant_id,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "verified": event.verified,
            "dedupe_key": event.dedupe_key,
            "policy": "NO_DIRECT_WRITE",
            "route": "canonical_event_workflow",
        }
