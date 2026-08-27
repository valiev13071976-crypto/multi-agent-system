"""Payment Gateway and Bank Gateways — domain layer above connectivity. No card processing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol

from payments.contracts import BankTransaction, PaymentRecord, RefundRecord, assert_no_card_data
from payments.errors import CardDataForbiddenError, ExternalUnconfirmedError
from payments.states import (
    PAY_AUTHORIZED,
    PAY_CAPTURED,
    PAY_CREATED,
    PAY_FAILED,
    PAY_PAID,
    PAY_PENDING,
    PAY_UNKNOWN,
    REF_CONFIRMED,
    REF_FAILED,
    REF_SUBMITTED,
    REF_UNKNOWN_EXTERNAL,
)
from security.tenant import normalize_tenant_id


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class GatewayConfirmation:
    external_id: str
    status: str
    system: str
    confirmed_at: datetime = field(default_factory=_utc)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        assert_no_card_data(self.metadata)


class PaymentGateway(Protocol):
    def get_payment_status(
        self, *, tenant_id: str, external_transaction_id: str
    ) -> GatewayConfirmation: ...

    def get_transaction(
        self, *, tenant_id: str, external_transaction_id: str
    ) -> PaymentRecord | None: ...

    def create_payment_intent(
        self,
        *,
        tenant_id: str,
        amount: float,
        currency: str,
        order_ref: str,
        idempotency_key: str,
        metadata: Mapping[str, object] | None = None,
    ) -> GatewayConfirmation: ...

    def verify_webhook(
        self, *, secret: str, body: bytes, signature_header: str
    ) -> bool: ...

    def get_refund_status(
        self, *, tenant_id: str, refund_external_id: str
    ) -> GatewayConfirmation: ...

    def prepare_refund(
        self,
        *,
        tenant_id: str,
        payment_external_id: str,
        amount: float,
        currency: str,
        idempotency_key: str,
    ) -> GatewayConfirmation: ...

    def execute_refund(
        self,
        *,
        tenant_id: str,
        payment_external_id: str,
        amount: float,
        currency: str,
        idempotency_key: str,
    ) -> GatewayConfirmation: ...

    def get_dispute_status(
        self, *, tenant_id: str, dispute_external_id: str
    ) -> GatewayConfirmation: ...


class BankGateway(Protocol):
    def get_account_metadata(self, *, tenant_id: str, account_ref: str) -> dict: ...

    def get_balance(self, *, tenant_id: str, account_ref: str) -> dict | None: ...

    def list_transactions(
        self,
        *,
        tenant_id: str,
        account_ref: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BankTransaction]: ...

    def get_statement_ref(
        self, *, tenant_id: str, account_ref: str, period_start: str, period_end: str
    ) -> dict: ...

    def lookup_transaction(
        self, *, tenant_id: str, external_bank_id: str
    ) -> BankTransaction | None: ...

    def lookup_incoming(
        self, *, tenant_id: str, amount: float, currency: str, reference: str = ""
    ) -> list[BankTransaction]: ...


class FakePaymentGateway:
    """In-memory acquiring stand-in — never stores card data; SoT for tests."""

    system = "payment_gateway"

    def __init__(self):
        self._payments: dict[tuple[str, str], PaymentRecord] = {}
        self._refunds: dict[tuple[str, str], dict] = {}
        self._ops: set[str] = set()
        self._disputes: dict[tuple[str, str], dict] = {}
        self.force_timeout_on_refund = False

    def seed_payment(self, payment: PaymentRecord) -> None:
        assert_no_card_data(payment.metadata)
        key = (normalize_tenant_id(payment.tenant_id), payment.external_transaction_id or payment.payment_id)
        self._payments[key] = payment

    def get_payment_status(
        self, *, tenant_id: str, external_transaction_id: str
    ) -> GatewayConfirmation:
        tenant = normalize_tenant_id(tenant_id)
        pay = self._payments.get((tenant, external_transaction_id))
        if pay is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        return GatewayConfirmation(
            external_id=external_transaction_id,
            status=pay.status,
            system=self.system,
        )

    def get_transaction(
        self, *, tenant_id: str, external_transaction_id: str
    ) -> PaymentRecord | None:
        return self._payments.get(
            (normalize_tenant_id(tenant_id), external_transaction_id)
        )

    def create_payment_intent(
        self,
        *,
        tenant_id: str,
        amount: float,
        currency: str,
        order_ref: str,
        idempotency_key: str,
        metadata: Mapping[str, object] | None = None,
    ) -> GatewayConfirmation:
        assert_no_card_data(metadata)
        if idempotency_key in self._ops:
            existing = next(
                (
                    p
                    for p in self._payments.values()
                    if p.tenant_id == normalize_tenant_id(tenant_id)
                    and order_ref in p.order_refs
                ),
                None,
            )
            if existing:
                return GatewayConfirmation(
                    external_id=existing.external_transaction_id,
                    status=existing.status,
                    system=self.system,
                )
        self._ops.add(idempotency_key)
        ext = f"pg-{uuid.uuid4().hex[:12]}"
        pay = PaymentRecord(
            payment_id=f"pay-{uuid.uuid4().hex[:10]}",
            tenant_id=tenant_id,
            provider=self.system,
            amount=float(amount),
            currency=currency,
            status=PAY_PENDING,
            external_transaction_id=ext,
            order_refs=(order_ref,) if order_ref else (),
            metadata=dict(metadata or {}),
        )
        self._payments[(normalize_tenant_id(tenant_id), ext)] = pay
        return GatewayConfirmation(external_id=ext, status=PAY_PENDING, system=self.system)

    def verify_webhook(self, *, secret: str, body: bytes, signature_header: str) -> bool:
        import hashlib
        import hmac

        if not secret or not signature_header:
            return False
        sig = signature_header.strip()
        if sig.startswith("sha256="):
            sig = sig[7:]
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, sig)

    def get_refund_status(
        self, *, tenant_id: str, refund_external_id: str
    ) -> GatewayConfirmation:
        row = self._refunds.get((normalize_tenant_id(tenant_id), refund_external_id))
        if row is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        return GatewayConfirmation(
            external_id=refund_external_id,
            status=str(row["status"]),
            system=self.system,
        )

    def prepare_refund(
        self,
        *,
        tenant_id: str,
        payment_external_id: str,
        amount: float,
        currency: str,
        idempotency_key: str,
    ) -> GatewayConfirmation:
        return GatewayConfirmation(
            external_id=f"prep-{idempotency_key[:16]}",
            status="PREPARED",
            system=self.system,
            metadata={"payment_external_id": payment_external_id, "amount": amount},
        )

    def execute_refund(
        self,
        *,
        tenant_id: str,
        payment_external_id: str,
        amount: float,
        currency: str,
        idempotency_key: str,
    ) -> GatewayConfirmation:
        if self.force_timeout_on_refund:
            raise ExternalUnconfirmedError("external_unconfirmed")
        tenant = normalize_tenant_id(tenant_id)
        if idempotency_key in self._ops:
            for (t, rid), row in self._refunds.items():
                if t == tenant and row.get("idempotency_key") == idempotency_key:
                    return GatewayConfirmation(
                        external_id=rid, status=str(row["status"]), system=self.system
                    )
        self._ops.add(idempotency_key)
        rid = f"rf-{uuid.uuid4().hex[:12]}"
        self._refunds[(tenant, rid)] = {
            "status": REF_CONFIRMED,
            "amount": amount,
            "currency": currency,
            "payment_external_id": payment_external_id,
            "idempotency_key": idempotency_key,
        }
        return GatewayConfirmation(external_id=rid, status=REF_CONFIRMED, system=self.system)

    def get_dispute_status(
        self, *, tenant_id: str, dispute_external_id: str
    ) -> GatewayConfirmation:
        row = self._disputes.get((normalize_tenant_id(tenant_id), dispute_external_id))
        if row is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        return GatewayConfirmation(
            external_id=dispute_external_id,
            status=str(row.get("status") or "open"),
            system=self.system,
            metadata={"amount": row.get("amount")},
        )

    def seed_dispute(self, *, tenant_id: str, dispute_id: str, amount: float, status: str = "open"):
        self._disputes[(normalize_tenant_id(tenant_id), dispute_id)] = {
            "amount": amount,
            "status": status,
        }

    def mark_payment_status(self, *, tenant_id: str, external_id: str, status: str) -> None:
        key = (normalize_tenant_id(tenant_id), external_id)
        pay = self._payments.get(key)
        if pay is None:
            return
        from dataclasses import replace

        self._payments[key] = replace(pay, status=status)


class FakeBankGateway:
    """Read-only bank stand-in."""

    system = "bank"

    def __init__(self):
        self._tx: dict[tuple[str, str], BankTransaction] = {}
        self._accounts: dict[tuple[str, str], dict] = {}

    def seed_transaction(self, tx: BankTransaction) -> None:
        assert_no_card_data(tx.metadata)
        key = (
            normalize_tenant_id(tx.tenant_id),
            tx.external_bank_id or tx.transaction_id,
        )
        self._tx[key] = tx

    def seed_account(self, *, tenant_id: str, account_ref: str, balance: float = 0.0):
        self._accounts[(normalize_tenant_id(tenant_id), account_ref)] = {
            "account_ref": account_ref,
            "balance": balance,
            "currency": "RUB",
        }

    def get_account_metadata(self, *, tenant_id: str, account_ref: str) -> dict:
        row = self._accounts.get((normalize_tenant_id(tenant_id), account_ref))
        return dict(row or {"account_ref": account_ref})

    def get_balance(self, *, tenant_id: str, account_ref: str) -> dict | None:
        return self._accounts.get((normalize_tenant_id(tenant_id), account_ref))

    def list_transactions(
        self,
        *,
        tenant_id: str,
        account_ref: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[BankTransaction]:
        tenant = normalize_tenant_id(tenant_id)
        out = [
            t
            for (tn, _), t in self._tx.items()
            if tn == tenant and t.account_ref == account_ref
        ]
        return out

    def get_statement_ref(
        self, *, tenant_id: str, account_ref: str, period_start: str, period_end: str
    ) -> dict:
        return {
            "account_ref": account_ref,
            "period_start": period_start,
            "period_end": period_end,
            "statement_ref": f"stmt-{account_ref}-{period_start}-{period_end}",
        }

    def lookup_transaction(
        self, *, tenant_id: str, external_bank_id: str
    ) -> BankTransaction | None:
        return self._tx.get((normalize_tenant_id(tenant_id), external_bank_id))

    def lookup_incoming(
        self, *, tenant_id: str, amount: float, currency: str, reference: str = ""
    ) -> list[BankTransaction]:
        tenant = normalize_tenant_id(tenant_id)
        cur = str(currency or "RUB").upper()
        out = []
        for (tn, _), t in self._tx.items():
            if tn != tenant or t.direction != "incoming":
                continue
            if t.currency != cur:
                continue
            if abs(t.amount - float(amount)) > 0.01:
                continue
            if reference and reference not in {
                t.document_ref,
                t.invoice_ref,
                t.order_ref,
                t.purpose,
            }:
                if reference not in (t.purpose or ""):
                    continue
            out.append(t)
        return out


class FakeBitrixPaymentBridge:
    """Bitrix/Aspro order payment field bridge — refs only."""

    system = "bitrix"

    def __init__(self):
        self._orders: dict[tuple[str, str], dict] = {}

    def seed_order(
        self,
        *,
        tenant_id: str,
        order_id: str,
        amount: float,
        currency: str = "RUB",
        invoice_number: str = "",
        inn: str = "",
        payment_status: str = "unconfirmed",
    ) -> None:
        self._orders[(normalize_tenant_id(tenant_id), order_id)] = {
            "order_id": order_id,
            "amount": amount,
            "currency": currency,
            "invoice_number": invoice_number or order_id,
            "inn": inn,
            "payment_status": payment_status,
        }

    def get_order(self, *, tenant_id: str, order_id: str) -> dict | None:
        return self._orders.get((normalize_tenant_id(tenant_id), order_id))
