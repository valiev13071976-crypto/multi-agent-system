"""Fake billing provider for deterministic closure tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid

from saas_product.providers.protocol import CheckoutSession, ProviderEvent


class FakeBillingProvider:
    name = "fake"

    def __init__(self, *, webhook_secret: str = "fake-webhook-secret"):
        self.webhook_secret = webhook_secret
        self._checkouts: dict[str, CheckoutSession] = {}
        self._events: dict[str, ProviderEvent] = {}
        self._sequence = 0

    def create_checkout(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        plan_version: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
    ) -> CheckoutSession:
        existing = next((c for c in self._checkouts.values() if c.idempotency_key == idempotency_key), None)
        if existing:
            return existing
        checkout = CheckoutSession(
            checkout_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            plan_id=plan_id,
            plan_version=plan_version,
            amount_minor=amount_minor,
            currency=currency,
            idempotency_key=idempotency_key,
            provider_customer_ref=f"cust-{tenant_id[:8]}",
            provider_subscription_ref=f"sub-{uuid.uuid4().hex[:12]}",
        )
        self._checkouts[checkout.checkout_id] = checkout
        return checkout

    def complete_checkout(self, checkout_id: str) -> ProviderEvent:
        checkout = self._checkouts[checkout_id]
        self._sequence += 1
        payload = {
            "checkout_id": checkout_id,
            "tenant_id": checkout.tenant_id,
            "plan_id": checkout.plan_id,
            "plan_version": checkout.plan_version,
            "amount_minor": checkout.amount_minor,
            "currency": checkout.currency,
            "subscription_ref": checkout.provider_subscription_ref,
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        event_id = f"evt-{uuid.uuid4().hex[:16]}"
        sig = self._sign(event_id, payload_hash)
        event = ProviderEvent(
            event_id=event_id,
            event_type="subscription.activated",
            tenant_id=checkout.tenant_id,
            checkout_id=checkout_id,
            subscription_ref=checkout.provider_subscription_ref,
            invoice_ref=f"inv-{uuid.uuid4().hex[:12]}",
            amount_minor=checkout.amount_minor,
            currency=checkout.currency,
            sequence=self._sequence,
            payload_hash=payload_hash,
            signature=sig,
        )
        self._events[event_id] = event
        checkout.status = "completed"
        return event

    def emit_event(
        self,
        *,
        event_type: str,
        tenant_id: str,
        checkout_id: str = "",
        subscription_ref: str = "",
        sequence: int | None = None,
        amount_minor: int = 0,
        currency: str = "USD",
    ) -> ProviderEvent:
        """Test helper for renewal/expiry/out-of-order events."""
        if sequence is None:
            self._sequence += 1
            sequence = self._sequence
        else:
            self._sequence = max(self._sequence, sequence)
        payload = {
            "event_type": event_type,
            "tenant_id": tenant_id,
            "checkout_id": checkout_id,
            "subscription_ref": subscription_ref,
            "sequence": sequence,
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        event_id = f"evt-{uuid.uuid4().hex[:16]}"
        event = ProviderEvent(
            event_id=event_id,
            event_type=event_type,
            tenant_id=tenant_id,
            checkout_id=checkout_id,
            subscription_ref=subscription_ref or f"sub-{uuid.uuid4().hex[:12]}",
            invoice_ref=f"inv-{uuid.uuid4().hex[:12]}",
            amount_minor=amount_minor,
            currency=currency,
            sequence=sequence,
            payload_hash=payload_hash,
            signature=self._sign(event_id, payload_hash),
        )
        self._events[event_id] = event
        return event

    def verify_webhook(self, *, event_id: str, signature: str, payload_hash: str) -> bool:
        expected = self._sign(event_id, payload_hash)
        return hmac.compare_digest(expected, signature)

    def get_event(self, event_id: str) -> ProviderEvent | None:
        return self._events.get(event_id)

    def get_checkout(self, checkout_id: str) -> CheckoutSession | None:
        return self._checkouts.get(checkout_id)

    def health_check(self) -> dict:
        return {"status": "healthy", "mode": "fake"}

    def _sign(self, event_id: str, payload_hash: str) -> str:
        raw = f"{event_id}:{payload_hash}".encode()
        return hmac.new(self.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()

    @staticmethod
    def generate_invitation_token() -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        return token, token_hash
