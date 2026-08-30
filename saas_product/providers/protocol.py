"""Billing provider protocol and shared types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class CheckoutSession:
    checkout_id: str
    tenant_id: str
    plan_id: str
    plan_version: str
    amount_minor: int
    currency: str
    idempotency_key: str
    status: str = "open"
    provider_customer_ref: str = ""
    provider_subscription_ref: str = ""
    provider_checkout_url: str = ""


@dataclass
class ProviderEvent:
    event_id: str
    event_type: str
    tenant_id: str
    checkout_id: str
    subscription_ref: str
    invoice_ref: str
    amount_minor: int
    currency: str
    sequence: int
    payload_hash: str
    signature: str


class BillingProvider(Protocol):
    name: str

    def create_checkout(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        plan_version: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
    ) -> CheckoutSession: ...

    def verify_webhook(self, *, event_id: str, signature: str, payload_hash: str) -> bool: ...

    def get_event(self, event_id: str) -> ProviderEvent | None: ...

    def get_checkout(self, checkout_id: str) -> CheckoutSession | None: ...

    def health_check(self) -> dict: ...
