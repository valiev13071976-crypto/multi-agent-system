"""Production billing adapters — Stripe-compatible sandbox/live."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient
from integrations.production.observability import ProviderObservability
from saas_product.providers.protocol import BillingProvider, CheckoutSession, ProviderEvent


@dataclass
class StripeBillingProvider:
    """Stripe billing adapter with webhook signature verification."""

    name: str = "stripe"
    secret_key: str = ""
    webhook_secret: str = ""
    mode: str = "test"
    timeout_seconds: float = 30.0
    obs: ProviderObservability | None = None
    _checkouts: dict[str, CheckoutSession] = field(default_factory=dict)
    _events: dict[str, ProviderEvent] = field(default_factory=dict)
    _sequence: int = 0
    _http: BoundedHttpClient | None = None

    def __post_init__(self) -> None:
        if not self.secret_key or not self.webhook_secret:
            raise ProductionProviderError(
                ProviderErrorCategory.CONFIGURATION_ERROR,
                message="stripe_credentials_missing",
                provider_id="stripe",
            )
        self._http = BoundedHttpClient(provider_id="stripe", timeout_seconds=self.timeout_seconds)

    @property
    def production_mode(self) -> bool:
        return self.mode == "live"

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
        started = time.monotonic()
        base = "https://api.stripe.com/v1"
        form = {
            "mode": "subscription",
            "success_url": "https://example.com/billing/success",
            "cancel_url": "https://example.com/billing/cancel",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_minor),
            "line_items[0][price_data][recurring][interval]": "month",
            "line_items[0][price_data][product_data][name]": f"{plan_id}:{plan_version}",
            "line_items[0][quantity]": "1",
            "metadata[tenant_id]": tenant_id,
            "metadata[plan_id]": plan_id,
            "metadata[plan_version]": plan_version,
            "metadata[idempotency_key]": idempotency_key,
        }
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Idempotency-Key": idempotency_key,
        }
        resp = self._http.request("POST", f"{base}/checkout/sessions", headers=headers, content=_encode_form(form))
        data = resp.json()
        checkout_id = str(data.get("id") or uuid.uuid4())
        checkout = CheckoutSession(
            checkout_id=checkout_id,
            tenant_id=tenant_id,
            plan_id=plan_id,
            plan_version=plan_version,
            amount_minor=amount_minor,
            currency=currency,
            idempotency_key=idempotency_key,
            provider_customer_ref=str(data.get("customer") or f"cust-{tenant_id[:8]}"),
            provider_subscription_ref=str(data.get("subscription") or f"sub-{uuid.uuid4().hex[:12]}"),
            provider_checkout_url=str(data.get("url") or ""),
        )
        self._checkouts[checkout_id] = checkout
        if self.obs:
            self.obs.emit(provider_id="stripe", operation="create_checkout", success=True, latency_ms=(time.monotonic() - started) * 1000)
        return checkout

    def verify_webhook(self, *, event_id: str, signature: str, payload_hash: str) -> bool:
        expected = self._sign(event_id, payload_hash)
        return hmac.compare_digest(expected, signature)

    def verify_stripe_signature(self, raw_body: bytes, signature_header: str, *, tolerance_seconds: int = 300) -> dict[str, Any]:
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in signature_header.split(",") if "=" in p}
        timestamp = parts.get("t")
        v1 = parts.get("v1")
        if not timestamp or not v1:
            raise ProductionProviderError(ProviderErrorCategory.WEBHOOK_VERIFICATION_FAILED, provider_id="stripe")
        if abs(time.time() - int(timestamp)) > tolerance_seconds:
            raise ProductionProviderError(ProviderErrorCategory.WEBHOOK_VERIFICATION_FAILED, message="timestamp_skew", provider_id="stripe")
        signed = f"{timestamp}.{raw_body.decode('utf-8')}".encode()
        expected = hmac.new(self.webhook_secret.encode(), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, v1):
            raise ProductionProviderError(ProviderErrorCategory.WEBHOOK_VERIFICATION_FAILED, provider_id="stripe")
        return json.loads(raw_body)

    def ingest_stripe_event(self, payload: dict[str, Any]) -> ProviderEvent:
        obj = payload.get("data", {}).get("object", {})
        metadata = obj.get("metadata") or {}
        checkout_id = str(obj.get("id") or metadata.get("checkout_id") or "")
        checkout = self._checkouts.get(checkout_id)
        tenant_id = str(metadata.get("tenant_id") or (checkout.tenant_id if checkout else ""))
        amount = int(obj.get("amount_total") or (checkout.amount_minor if checkout else 0))
        currency = str(obj.get("currency") or (checkout.currency if checkout else "usd")).upper()
        self._sequence += 1
        event_body = {
            "checkout_id": checkout_id,
            "tenant_id": tenant_id,
            "amount_minor": amount,
            "currency": currency,
        }
        payload_hash = hashlib.sha256(json.dumps(event_body, sort_keys=True).encode()).hexdigest()
        event_id = str(payload.get("id") or f"evt-{uuid.uuid4().hex[:16]}")
        sig = self._sign(event_id, payload_hash)
        event = ProviderEvent(
            event_id=event_id,
            event_type=str(payload.get("type") or "checkout.session.completed"),
            tenant_id=tenant_id,
            checkout_id=checkout_id,
            subscription_ref=str(obj.get("subscription") or f"sub-{uuid.uuid4().hex[:12]}"),
            invoice_ref=str(obj.get("invoice") or f"inv-{uuid.uuid4().hex[:12]}"),
            amount_minor=amount,
            currency=currency,
            sequence=self._sequence,
            payload_hash=payload_hash,
            signature=sig,
        )
        self._events[event_id] = event
        if checkout:
            checkout.status = "completed"
        return event

    def get_event(self, event_id: str) -> ProviderEvent | None:
        return self._events.get(event_id)

    def get_checkout(self, checkout_id: str) -> CheckoutSession | None:
        return self._checkouts.get(checkout_id)

    def health_check(self) -> dict:
        started = time.monotonic()
        try:
            resp = self._http.request(
                "GET",
                "https://api.stripe.com/v1/balance",
                headers={"Authorization": f"Bearer {self.secret_key}"},
            )
            ok = resp.status_code == 200
            if self.obs:
                self.obs.emit(provider_id="stripe", operation="health", success=ok, latency_ms=(time.monotonic() - started) * 1000)
            return {"status": "healthy" if ok else "degraded", "mode": self.mode, "latency_ms": round((time.monotonic() - started) * 1000, 2)}
        except ProductionProviderError as exc:
            if self.obs:
                self.obs.emit(provider_id="stripe", operation="health", success=False, error_category=exc.category.value)
            return {"status": "unhealthy", "error_category": exc.category.value}

    def _sign(self, event_id: str, payload_hash: str) -> str:
        raw = f"{event_id}:{payload_hash}".encode()
        return hmac.new(self.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()

    def close(self) -> None:
        if self._http:
            self._http.close()


def _encode_form(data: dict[str, str]) -> bytes:
    from urllib.parse import urlencode

    return urlencode(data).encode()


def build_billing_provider(env: dict) -> BillingProvider:
    from saas_product.providers.fake_billing import FakeBillingProvider

    provider = str(env.get("SAAS_BILLING_PROVIDER") or "fake").strip().lower()
    if provider == "fake":
        secret = str(env.get("SAAS_BILLING_WEBHOOK_SECRET") or "fake-webhook-secret")
        return FakeBillingProvider(webhook_secret=secret)
    if provider == "stripe":
        key = str(env.get("STRIPE_SECRET_KEY") or "").strip()
        wh = str(env.get("SAAS_BILLING_WEBHOOK_SECRET") or env.get("STRIPE_WEBHOOK_SECRET") or "").strip()
        mode = str(env.get("SAAS_BILLING_MODE") or "test").strip().lower()
        if not key or not wh:
            raise ProductionProviderError(
                ProviderErrorCategory.CONFIGURATION_ERROR,
                message="stripe_not_configured",
                provider_id="stripe",
            )
        return StripeBillingProvider(secret_key=key, webhook_secret=wh, mode=mode)
    raise ProductionProviderError(
        ProviderErrorCategory.CONFIGURATION_ERROR,
        message=f"unknown_billing_provider:{provider}",
        provider_id=provider,
    )
