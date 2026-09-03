"""Billing service — checkout, webhooks, subscription lifecycle."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from saas_product.entitlements import EntitlementService
from saas_product.errors import SAAS_CONFLICT, SAAS_NOT_FOUND, SAAS_WEBHOOK_INVALID, SaaSError
from saas_product.models import (
    INVOICE_PAID,
    SUB_ACTIVE,
    SUB_CANCEL_PENDING,
    SUB_CANCELED,
    SUB_PAST_DUE,
    BillingEventRecord,
    InvoiceRecord,
    SubscriptionRecord,
)
from saas_product.plans import get_plan
from saas_product.providers.fake_billing import FakeBillingProvider
from saas_product.providers.protocol import BillingProvider, ProviderEvent


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class BillingService:
    def __init__(self, *, store, provider: BillingProvider | None = None, entitlements: EntitlementService | None = None):
        self.store = store
        self.provider = provider or FakeBillingProvider()
        self.entitlements = entitlements or EntitlementService()
        self._processed_checkouts: set[str] = set()
        self._last_sequence: dict[str, int] = {}  # tenant_id -> last applied sequence

    def create_checkout(self, *, tenant_id: str, plan_id: str, plan_version: str, idempotency_key: str) -> dict:
        plan = get_plan(plan_id, plan_version)
        if plan is None:
            raise SaaSError(SAAS_NOT_FOUND, message="Plan not found.")
        checkout = self.provider.create_checkout(
            tenant_id=tenant_id,
            plan_id=plan_id,
            plan_version=plan_version,
            amount_minor=plan.price_minor,
            currency=plan.currency,
            idempotency_key=idempotency_key,
        )
        return {
            "checkout_id": checkout.checkout_id,
            "amount_minor": checkout.amount_minor,
            "currency": checkout.currency,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "status": checkout.status,
        }

    def process_webhook(self, *, event_id: str, signature: str, payload_hash: str) -> dict:
        if not self.provider.verify_webhook(event_id=event_id, signature=signature, payload_hash=payload_hash):
            raise SaaSError(SAAS_WEBHOOK_INVALID)
        existing = self.store.get_billing_event(event_id)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise SaaSError(SAAS_CONFLICT, message="Conflicting billing event.")
            return {"status": "already_processed", "event_id": event_id, "idempotent": True}
        event = self.provider.get_event(event_id)
        if event is None:
            raise SaaSError(SAAS_NOT_FOUND, message="Unknown billing event.")

        last_seq = self._last_sequence.get(event.tenant_id, 0)
        if event.sequence and event.sequence < last_seq:
            now = _utc()
            self.store.record_billing_event(
                BillingEventRecord(
                    event_id=event_id,
                    provider=self.provider.name,
                    event_type=event.event_type,
                    tenant_id=event.tenant_id,
                    subscription_id="",
                    payload_hash=payload_hash,
                    processed_at=now,
                    result="ignored_out_of_order",
                )
            )
            return {"status": "ignored_out_of_order", "event_id": event_id, "last_sequence": last_seq}

        now = _utc()
        end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        checkout = self.provider.get_checkout(event.checkout_id) if event.checkout_id else None
        if checkout is None and event.event_type not in {
            "subscription.expired",
            "subscription.past_due",
            "subscription.renewed",
        }:
            raise SaaSError(SAAS_NOT_FOUND, message="Checkout not found.")

        existing_sub = self.store.get_subscription_for_tenant(event.tenant_id)
        invoice_id = ""
        if event.event_type == "subscription.expired":
            if existing_sub is None:
                self.store.record_billing_event(
                    BillingEventRecord(
                        event_id=event_id,
                        provider=self.provider.name,
                        event_type=event.event_type,
                        tenant_id=event.tenant_id,
                        subscription_id="",
                        payload_hash=payload_hash,
                        processed_at=now,
                        result="ignored_no_subscription",
                    )
                )
                return {"status": "ignored_no_subscription", "event_id": event_id}
            sub = SubscriptionRecord(
                **{**existing_sub.__dict__, "status": SUB_CANCELED, "updated_at": now, "current_period_end": now}
            )
            self.store.save_subscription(sub)
        elif event.event_type == "subscription.past_due":
            if existing_sub is None:
                raise SaaSError(SAAS_NOT_FOUND)
            sub = SubscriptionRecord(**{**existing_sub.__dict__, "status": SUB_PAST_DUE, "updated_at": now})
            self.store.save_subscription(sub)
        else:
            sub = SubscriptionRecord(
                subscription_id=existing_sub.subscription_id if existing_sub else str(uuid.uuid4()),
                tenant_id=event.tenant_id,
                provider=self.provider.name,
                provider_customer_ref=(
                    checkout.provider_customer_ref
                    if checkout
                    else (existing_sub.provider_customer_ref if existing_sub else "")
                ),
                provider_subscription_ref=event.subscription_ref,
                plan_id=checkout.plan_id if checkout else (existing_sub.plan_id if existing_sub else "starter"),
                plan_version=checkout.plan_version if checkout else (existing_sub.plan_version if existing_sub else "2026-01"),
                status=SUB_ACTIVE,
                current_period_start=now,
                current_period_end=end,
                version=existing_sub.version if existing_sub else 1,
            )
            self.store.save_subscription(sub)
            invoice = InvoiceRecord(
                invoice_id=str(uuid.uuid4()),
                tenant_id=event.tenant_id,
                subscription_id=sub.subscription_id,
                provider_invoice_ref=event.invoice_ref,
                amount_minor=event.amount_minor,
                currency=event.currency,
                status=INVOICE_PAID,
                created_at=now,
                paid_at=now,
            )
            self.store.save_invoice(invoice)
            invoice_id = invoice.invoice_id

        if event.sequence:
            self._last_sequence[event.tenant_id] = max(last_seq, event.sequence)

        self.store.record_billing_event(
            BillingEventRecord(
                event_id=event_id,
                provider=self.provider.name,
                event_type=event.event_type,
                tenant_id=event.tenant_id,
                subscription_id=sub.subscription_id,
                payload_hash=payload_hash,
                processed_at=now,
                result="ok",
            )
        )
        return {"status": "processed", "subscription_id": sub.subscription_id, "invoice_id": invoice_id}

    def cancel_subscription(self, *, tenant_id: str, at_period_end: bool = True) -> dict:
        sub = self.store.get_subscription_for_tenant(tenant_id)
        if sub is None:
            raise SaaSError(SAAS_NOT_FOUND)
        new_status = SUB_CANCEL_PENDING if at_period_end else SUB_CANCELED
        updated = SubscriptionRecord(
            **{**sub.__dict__, "status": new_status, "cancel_at_period_end": at_period_end, "updated_at": _utc()}
        )
        self.store.save_subscription(updated)
        return {"subscription_id": updated.subscription_id, "status": updated.status}

    def simulate_paid_checkout(self, checkout_id: str) -> ProviderEvent:
        """Test helper — complete checkout and return provider event."""
        event = self.provider.complete_checkout(checkout_id)
        self.process_webhook(event_id=event.event_id, signature=event.signature, payload_hash=event.payload_hash)
        return event

    def reconcile(self, *, tenant_id: str) -> dict:
        sub = self.store.get_subscription_for_tenant(tenant_id)
        return {"tenant_id": tenant_id, "local_status": sub.status if sub else "none", "provider": self.provider.name}
