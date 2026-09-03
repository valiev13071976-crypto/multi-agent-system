"""Payment method recurring-use controls (no card data)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from accounts.errors import AccountsError
from accounts.models import AccountsAuditEvent, PaymentMethodControl

PAYMENT_METHOD_USAGE_ALLOWED = "PAYMENT_METHOD_USAGE_ALLOWED"
PAYMENT_METHOD_USAGE_REVOKED = "PAYMENT_METHOD_USAGE_REVOKED"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaymentMethodService:
    def __init__(self, *, store):
        self.store = store

    def allow(self, *, tenant_id: str, user_id: str, provider: str, provider_reference: str) -> PaymentMethodControl:
        ref = (provider_reference or "").replace(" ", "")
        if not ref:
            raise AccountsError("PROVIDER_REF_REQUIRED")
        if ref.isdigit() and len(ref) >= 12:
            raise AccountsError("CARD_DATA_FORBIDDEN", "Full card numbers must not be stored.")
        if len(ref) in {3, 4} and ref.isdigit():
            raise AccountsError("CARD_DATA_FORBIDDEN", "CVV must not be stored.")
        existing = self.store.get_payment_method(
            tenant_id=tenant_id, provider=provider, provider_reference=provider_reference
        )
        if existing and existing.usage_status == PAYMENT_METHOD_USAGE_ALLOWED:
            return existing
        control = PaymentMethodControl(
            control_id=existing.control_id if existing else str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            provider=provider,
            provider_reference=provider_reference,
            usage_status=PAYMENT_METHOD_USAGE_ALLOWED,
            created_at=existing.created_at if existing else _iso(),
        )
        return self.store.upsert_payment_method(control)

    def revoke(self, *, tenant_id: str, user_id: str, provider: str, provider_reference: str, source: str) -> PaymentMethodControl:
        existing = self.store.get_payment_method(
            tenant_id=tenant_id, provider=provider, provider_reference=provider_reference
        )
        if existing and existing.usage_status == PAYMENT_METHOD_USAGE_REVOKED:
            return existing  # idempotent
        control = PaymentMethodControl(
            control_id=existing.control_id if existing else str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            provider=provider,
            provider_reference=provider_reference,
            usage_status=PAYMENT_METHOD_USAGE_REVOKED,
            created_at=existing.created_at if existing else _iso(),
            revoked_at=_iso(),
            revocation_source=source,
        )
        self.store.upsert_payment_method(control)
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_iso(),
                actor_id=user_id,
                target_id=control.control_id,
                tenant_id=tenant_id,
                action="payment_method.revoked",
                result="ok",
                metadata={"provider": provider},
            )
        )
        return control

    def may_charge(self, *, tenant_id: str, provider: str, provider_reference: str) -> bool:
        control = self.store.get_payment_method(
            tenant_id=tenant_id, provider=provider, provider_reference=provider_reference
        )
        if control is None:
            return False
        return control.usage_status == PAYMENT_METHOD_USAGE_ALLOWED
