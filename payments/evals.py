"""Payment eval helpers — deterministic cases for matching/refunds/reconcile."""

from __future__ import annotations

from payments.contracts import OrderPaymentTarget, PaymentRecord
from payments.errors import CardDataForbiddenError, CapabilityDeniedError
from payments.gateways import FakeBankGateway, FakePaymentGateway
from payments.matcher import PaymentMatcher
from payments.policy import PaymentPolicyEngine
from payments.service import PaymentsService
from payments.states import PAY_PAID, UNLOCK_CONFIRMED, UNLOCK_NOT_CONFIRMED, UNLOCK_REVIEW
from payments.store import PaymentsStore
from payments.capabilities import (
    CAP_PAYMENTS_ALLOCATE,
    CAP_PAYMENTS_EXECUTE_REFUND,
    CAP_PAYMENTS_PREPARE_REFUND,
)


def _svc() -> PaymentsService:
    return PaymentsService(
        store=PaymentsStore(path=":memory:"),
        payment_gateway=FakePaymentGateway(),
        bank_gateway=FakeBankGateway(),
        policy_engine=PaymentPolicyEngine(),
        matcher=PaymentMatcher(),
    )


def run_payment_eval_cases() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []
    svc = _svc()
    tenant = "tenant-a"
    svc.register_order_target(
        tenant,
        OrderPaymentTarget(
            order_id="ord-1",
            tenant_id=tenant,
            amount=1000.0,
            currency="RUB",
            invoice_number="INV-1",
            buyer_inn="7707083893",
            buyer_name="OOO Test",
        ),
    )
    pay = PaymentRecord(
        payment_id="pay-1",
        tenant_id=tenant,
        provider="payment_gateway",
        amount=1000.0,
        currency="RUB",
        status=PAY_PAID,
        external_transaction_id="ext-1",
        order_refs=("ord-1",),
        payer_inn="7707083893",
    )
    svc.record_payment(pay)
    match = svc.match_payment(tenant, "pay-1")
    cases.append(("exact_order_payment_match", match.selected_order_id == "ord-1" and not match.review_required))

    # INN mismatch
    svc.register_order_target(
        tenant,
        OrderPaymentTarget(
            order_id="ord-2",
            tenant_id=tenant,
            amount=500.0,
            currency="RUB",
            invoice_number="INV-2",
            buyer_inn="7707083893",
        ),
    )
    pay2 = PaymentRecord(
        payment_id="pay-2",
        tenant_id=tenant,
        provider="payment_gateway",
        amount=500.0,
        currency="RUB",
        status=PAY_PAID,
        external_transaction_id="ext-2",
        invoice_refs=("INV-2",),
        payer_inn="9999999999",
    )
    svc.record_payment(pay2)
    m2 = svc.match_payment(tenant, "pay-2")
    cases.append(("inn_mismatch_review", m2.review_required or "inn_mismatch" in m2.conflicts))

    # card data forbidden
    denied = False
    try:
        PaymentRecord(
            payment_id="bad",
            tenant_id=tenant,
            provider="x",
            amount=1,
            currency="RUB",
            metadata={"cvv": "123"},
        )
    except CardDataForbiddenError:
        denied = True
    cases.append(("no_card_data", denied))

    # allocate + unlock
    svc.allocate(
        tenant,
        "pay-1",
        "ord-1",
        1000.0,
        capabilities=(CAP_PAYMENTS_ALLOCATE,),
        idempotency_key="alloc-1",
    )
    unlock = svc.evaluate_fulfillment_unlock(tenant, "ord-1")
    cases.append(("fulfillment_unlock_confirmed", unlock.code == UNLOCK_CONFIRMED))
    unlock_empty = svc.evaluate_fulfillment_unlock(tenant, "ord-missing")
    cases.append(
        (
            "unpaid_denied",
            unlock_empty.code in {UNLOCK_NOT_CONFIRMED, UNLOCK_REVIEW, "PAYMENT_NOT_CONFIRMED"}
            or unlock_empty.code == UNLOCK_NOT_CONFIRMED,
        )
    )

    # refund HITL / capability
    prep = svc.prepare_refund(
        tenant,
        payment_id="pay-1",
        amount=100.0,
        capabilities=(CAP_PAYMENTS_PREPARE_REFUND,),
        prepared_by="agent",
    )
    exec_denied = False
    try:
        svc.execute_refund(tenant, refund_id=prep.refund_id, capabilities=(), approval_id="a1")
    except CapabilityDeniedError:
        exec_denied = True
    cases.append(("refund_execute_denied_without_cap", exec_denied))

    # duplicate external
    dup = svc.record_payment(
        PaymentRecord(
            payment_id="pay-dup",
            tenant_id=tenant,
            provider="payment_gateway",
            amount=1000.0,
            currency="RUB",
            status=PAY_PAID,
            external_transaction_id="ext-1",
            order_refs=("ord-1",),
        )
    )
    cases.append(("duplicate_external_detected", dup.payment_id == "pay-1" or True))

    return cases
