"""Payments & Reconciliation Platform — acceptance tests (unittest, deterministic)."""

from __future__ import annotations

import hashlib
import hmac
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from commerce.contracts import CommerceOrder, CommerceOrderLine
from commerce.service import CommerceService, _order_payload
from commerce.states import ORDER_NEW
from commerce.store import CommerceStore
from payments.capabilities import (
    CAP_PAYMENTS_ALLOCATE,
    CAP_PAYMENTS_EXECUTE_REFUND,
    CAP_PAYMENTS_PREPARE_REFUND,
)
from payments.contracts import (
    BankTransaction,
    OrderPaymentTarget,
    PaymentRecord,
)
from payments.errors import (
    CardDataForbiddenError,
    CapabilityDeniedError,
    ExternalUnconfirmedError,
    InvalidTransitionError,
    PolicyDeniedError,
)
from payments.evals import run_payment_eval_cases
from payments.gateways import FakeBankGateway, FakePaymentGateway
from payments.matcher import PaymentMatcher
from payments.normalize import normalize_event_type
from payments.policy import PaymentPolicyEngine
from payments.reconcile import PaymentsReconciliationEngine
from payments.service import PaymentsService
from payments.states import (
    PAY_CREATED,
    PAY_FAILED,
    PAY_PAID,
    PAY_PENDING,
    REF_AWAITING_APPROVAL,
    REF_CONFIRMED,
    REF_FAILED,
    REF_PREPARED,
    REF_UNKNOWN_EXTERNAL,
    UNLOCK_CONFIRMED,
    UNLOCK_NOT_CONFIRMED,
    UNLOCK_PARTIAL,
    UNLOCK_REVIEW,
    assert_transition,
)
from payments.store import PaymentsStore
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets
from workflow.models import utc_now


TENANT = "tenant-a"
CAPS_ALLOC = (CAP_PAYMENTS_ALLOCATE,)
CAPS_PREP = (CAP_PAYMENTS_PREPARE_REFUND,)
CAPS_EXEC = (CAP_PAYMENTS_EXECUTE_REFUND,)
CAPS_REFUND_FULL = (CAP_PAYMENTS_PREPARE_REFUND, CAP_PAYMENTS_EXECUTE_REFUND)


def _svc(
    *,
    store: PaymentsStore | None = None,
    payment_gateway: FakePaymentGateway | None = None,
    bank_gateway: FakeBankGateway | None = None,
    commerce_service=None,
    hitl=None,
) -> PaymentsService:
    return PaymentsService(
        store=store or PaymentsStore(path=":memory:"),
        payment_gateway=payment_gateway or FakePaymentGateway(),
        bank_gateway=bank_gateway or FakeBankGateway(),
        policy_engine=PaymentPolicyEngine(),
        matcher=PaymentMatcher(),
        recon_engine=PaymentsReconciliationEngine(),
        commerce_service=commerce_service,
        hitl=hitl,
    )


def _pay(
    *,
    payment_id: str | None = None,
    amount: float = 1000.0,
    status: str = PAY_PAID,
    external_transaction_id: str | None = None,
    order_refs: tuple[str, ...] = (),
    invoice_refs: tuple[str, ...] = (),
    payer_inn: str = "",
    payer_name: str = "",
    tenant_id: str = TENANT,
    **kwargs,
) -> PaymentRecord:
    return PaymentRecord(
        payment_id=payment_id or f"pay-{uuid.uuid4().hex[:10]}",
        tenant_id=tenant_id,
        provider="payment_gateway",
        amount=amount,
        currency="RUB",
        status=status,
        external_transaction_id=external_transaction_id
        or f"ext-{uuid.uuid4().hex[:10]}",
        order_refs=order_refs,
        invoice_refs=invoice_refs,
        payer_inn=payer_inn,
        payer_name=payer_name,
        **kwargs,
    )


def _target(
    order_id: str,
    *,
    amount: float = 1000.0,
    invoice_number: str = "",
    buyer_inn: str = "7707083893",
    buyer_name: str = "OOO Test",
    fulfillment_state: str = "",
    shipment_started: bool = False,
    marking_incomplete: bool = False,
    fiscal_receipt_ref: str = "",
    cancelled: bool = False,
    tenant_id: str = TENANT,
) -> OrderPaymentTarget:
    return OrderPaymentTarget(
        order_id=order_id,
        tenant_id=tenant_id,
        amount=amount,
        currency="RUB",
        invoice_number=invoice_number or f"INV-{order_id}",
        buyer_inn=buyer_inn,
        buyer_name=buyer_name,
        fulfillment_state=fulfillment_state,
        shipment_started=shipment_started,
        marking_incomplete=marking_incomplete,
        fiscal_receipt_ref=fiscal_receipt_ref,
        cancelled=cancelled,
    )


def _env(path: str, **extra) -> dict:
    base = {
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SIDE_EFFECT_DB_PATH": path,
        "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
        "PAYMENTS_ENABLED": "true",
        "PAYMENTS_USE_SHARED_DB": "true",
        "INTEGRATION_SECRETS_BACKEND": "memory",
    }
    base.update(extra)
    return base


class FakeHitl:
    def __init__(self):
        self.calls: list = []

    def request_approval(self, action, decision, *, requested_by, now=None):
        rec = SimpleNamespace(approval_id=f"appr-{uuid.uuid4().hex[:8]}")
        self.calls.append(
            {
                "action": action,
                "decision": decision,
                "requested_by": requested_by,
                "approval_id": rec.approval_id,
            }
        )
        return rec


class FakeCommerceUnlock:
    """Minimal commerce stand-in capturing update_payment_reference calls."""

    def __init__(self):
        self.calls: list[dict] = []

    def update_payment_reference(
        self,
        *,
        tenant_id: str,
        order_id: str,
        payment_status: str,
        payment_refs=None,
        unlock_code: str = "",
    ):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "order_id": order_id,
                "payment_status": payment_status,
                "payment_refs": list(payment_refs or []),
                "unlock_code": unlock_code,
            }
        )
        return SimpleNamespace(
            order_id=order_id,
            payment_status=payment_status,
            payment_state_ref=(payment_refs or [""])[0] if payment_refs else "",
        )


# ---------------------------------------------------------------------------
# 1. Contracts / state
# ---------------------------------------------------------------------------


class ContractsStateTests(unittest.TestCase):
    def test_valid_and_invalid_payment_transitions(self):
        assert_transition("payment", PAY_CREATED, PAY_PENDING)
        assert_transition("payment", PAY_PENDING, PAY_PAID)
        assert_transition("payment", PAY_PAID, PAY_PAID)  # no-op
        with self.assertRaises(InvalidTransitionError):
            assert_transition("payment", PAY_CREATED, REF_CONFIRMED)
        with self.assertRaises(InvalidTransitionError):
            assert_transition("payment", PAY_FAILED, PAY_PAID)

    def test_card_data_forbidden_on_payment_record(self):
        with self.assertRaises(CardDataForbiddenError):
            PaymentRecord(
                payment_id="bad-cvv",
                tenant_id=TENANT,
                provider="x",
                amount=1.0,
                currency="RUB",
                metadata={"cvv": "123"},
            )
        with self.assertRaises(CardDataForbiddenError):
            PaymentRecord(
                payment_id="bad-pan",
                tenant_id=TENANT,
                provider="x",
                amount=1.0,
                currency="RUB",
                metadata={"pan": "4111111111111111"},
            )


# ---------------------------------------------------------------------------
# 2. Webhooks
# ---------------------------------------------------------------------------


class WebhookTests(unittest.TestCase):
    def test_process_webhook_idempotent_duplicate_event_id(self):
        svc = _svc()
        payload = {
            "external_transaction_id": "ext-wh-1",
            "amount": 500.0,
            "currency": "RUB",
            "order_ref": "ord-wh-1",
        }
        first = svc.process_webhook_event(
            TENANT,
            "payment_gateway",
            "evt-1",
            "payment.succeeded",
            payload,
        )
        self.assertEqual(first["status"], "processed")
        self.assertEqual(first["canonical_event_type"], "payment.succeeded")
        self.assertEqual(first["payment_status"], PAY_PAID)
        second = svc.process_webhook_event(
            TENANT,
            "payment_gateway",
            "evt-1",
            "payment.succeeded",
            payload,
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["event_id"], "evt-1")
        self.assertEqual(svc.metrics["webhooks_duplicate"], 1)

    def test_normalize_payment_succeeded(self):
        self.assertEqual(normalize_event_type("payment.succeeded"), "payment.succeeded")
        self.assertEqual(normalize_event_type("payment_intent.succeeded"), "payment.succeeded")
        self.assertEqual(normalize_event_type("charge.succeeded"), "payment.succeeded")

    def test_fake_gateway_verify_webhook_true_false(self):
        gw = FakePaymentGateway()
        secret = "whsec_test"
        body = b'{"id":"evt-x","type":"payment.succeeded"}'
        good = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        self.assertTrue(
            gw.verify_webhook(secret=secret, body=body, signature_header=f"sha256={good}")
        )
        self.assertTrue(gw.verify_webhook(secret=secret, body=body, signature_header=good))
        self.assertFalse(
            gw.verify_webhook(secret=secret, body=body, signature_header="sha256=deadbeef")
        )
        self.assertFalse(gw.verify_webhook(secret="", body=body, signature_header=good))
        self.assertFalse(gw.verify_webhook(secret=secret, body=body, signature_header=""))


# ---------------------------------------------------------------------------
# 3. Gateway
# ---------------------------------------------------------------------------


class GatewayTests(unittest.TestCase):
    def test_get_payment_status_and_missing(self):
        gw = FakePaymentGateway()
        pay = _pay(external_transaction_id="ext-gw-1", status=PAY_PAID)
        gw.seed_payment(pay)
        conf = gw.get_payment_status(tenant_id=TENANT, external_transaction_id="ext-gw-1")
        self.assertEqual(conf.external_id, "ext-gw-1")
        self.assertEqual(conf.status, PAY_PAID)
        with self.assertRaises(ExternalUnconfirmedError):
            gw.get_payment_status(tenant_id=TENANT, external_transaction_id="missing")

    def test_bank_list_and_lookup(self):
        bank = FakeBankGateway()
        tx = BankTransaction(
            transaction_id="btx-1",
            tenant_id=TENANT,
            account_ref="acc-1",
            amount=1000.0,
            currency="RUB",
            direction="incoming",
            external_bank_id="bank-ext-1",
            purpose="оплата по счету INV-99",
            invoice_ref="INV-99",
        )
        bank.seed_transaction(tx)
        bank.seed_account(tenant_id=TENANT, account_ref="acc-1", balance=5000.0)
        listed = bank.list_transactions(tenant_id=TENANT, account_ref="acc-1")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].external_bank_id, "bank-ext-1")
        found = bank.lookup_transaction(tenant_id=TENANT, external_bank_id="bank-ext-1")
        self.assertIsNotNone(found)
        self.assertEqual(found.transaction_id, "btx-1")
        self.assertIsNone(
            bank.lookup_transaction(tenant_id=TENANT, external_bank_id="nope")
        )
        with self.assertRaises(ExternalUnconfirmedError):
            FakePaymentGateway().get_payment_status(
                tenant_id=TENANT, external_transaction_id="absent"
            )


# ---------------------------------------------------------------------------
# 4. Matching
# ---------------------------------------------------------------------------


class MatchingTests(unittest.TestCase):
    def test_explicit_order_ref_match(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-m1", amount=1000.0))
        pay = svc.record_payment(
            _pay(order_refs=("ord-m1",), amount=1000.0, payer_inn="7707083893")
        )
        match = svc.match_payment(TENANT, pay.payment_id)
        self.assertEqual(match.selected_order_id, "ord-m1")
        self.assertFalse(match.review_required)
        self.assertIn("explicit_order_ref", match.evidence)

    def test_invoice_match(self):
        svc = _svc()
        svc.register_order_target(
            TENANT, _target("ord-inv", amount=750.0, invoice_number="INV-750")
        )
        pay = svc.record_payment(
            _pay(
                amount=750.0,
                invoice_refs=("INV-750",),
                payer_inn="7707083893",
            )
        )
        match = svc.match_payment(TENANT, pay.payment_id)
        self.assertEqual(match.selected_order_id, "ord-inv")
        self.assertEqual(match.selected_invoice_id, "INV-750")
        self.assertFalse(match.review_required)

    def test_inn_mismatch_review(self):
        svc = _svc()
        svc.register_order_target(
            TENANT, _target("ord-inn", amount=500.0, invoice_number="INV-INN")
        )
        pay = svc.record_payment(
            _pay(
                amount=500.0,
                invoice_refs=("INV-INN",),
                payer_inn="9999999999",
            )
        )
        match = svc.match_payment(TENANT, pay.payment_id)
        self.assertTrue(match.review_required)
        self.assertIn("inn_mismatch", match.conflicts)

    def test_amount_match_evidence(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-amt", amount=2000.0))
        pay = svc.record_payment(
            _pay(order_refs=("ord-amt",), amount=2000.0, payer_inn="7707083893")
        )
        match = svc.match_payment(TENANT, pay.payment_id)
        self.assertIn("amount", match.evidence)
        self.assertEqual(match.evidence["amount"], 2000.0)


# ---------------------------------------------------------------------------
# 5. B2B
# ---------------------------------------------------------------------------


class B2BTests(unittest.TestCase):
    def test_wrong_payer_inn_review(self):
        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target("ord-b2b", amount=10000.0, buyer_inn="7707083893", invoice_number="INV-B2B"),
        )
        pay = svc.record_payment(
            _pay(
                amount=10000.0,
                invoice_refs=("INV-B2B",),
                payer_inn="1234567890",
                payer_name="Wrong LLC",
            )
        )
        match = svc.match_payment(TENANT, pay.payment_id)
        self.assertTrue(match.review_required)
        self.assertIn("inn_mismatch", match.conflicts)

    def test_payment_purpose_invoice_bank_match(self):
        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target("ord-pur", amount=3000.0, invoice_number="INV-PUR-77", buyer_inn="7707083893"),
        )
        tx = BankTransaction(
            transaction_id="btx-pur",
            tenant_id=TENANT,
            account_ref="acc-1",
            amount=3000.0,
            currency="RUB",
            direction="incoming",
            external_bank_id="bank-pur-1",
            purpose="Оплата по счету INV-PUR-77 за услуги",
            payer_inn="7707083893",
        )
        svc.ingest_bank_transactions(TENANT, [tx])
        match = svc.match_bank_tx(TENANT, "btx-pur")
        self.assertEqual(match.selected_order_id, "ord-pur")
        self.assertFalse(match.review_required)

    def test_one_payment_multiple_invoices_two_allocates(self):
        svc = _svc()
        svc.register_order_target(
            TENANT, _target("ord-a", amount=400.0, invoice_number="INV-A")
        )
        svc.register_order_target(
            TENANT, _target("ord-b", amount=600.0, invoice_number="INV-B")
        )
        pay = svc.record_payment(_pay(amount=1000.0, payment_id="pay-multi-inv"))
        a1 = svc.allocate(
            TENANT,
            pay.payment_id,
            "ord-a",
            400.0,
            invoice_id="INV-A",
            capabilities=CAPS_ALLOC,
            idempotency_key="alloc-a",
        )
        a2 = svc.allocate(
            TENANT,
            pay.payment_id,
            "ord-b",
            600.0,
            invoice_id="INV-B",
            capabilities=CAPS_ALLOC,
            idempotency_key="alloc-b",
        )
        self.assertEqual(a1.allocated_amount, 400.0)
        self.assertEqual(a2.allocated_amount, 600.0)
        self.assertEqual(svc.allocated_total_for_order(TENANT, "ord-a"), 400.0)
        self.assertEqual(svc.allocated_total_for_order(TENANT, "ord-b"), 600.0)

    def test_multiple_payments_one_order(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-mp", amount=1000.0))
        p1 = svc.record_payment(_pay(amount=400.0, payment_id="pay-mp-1"))
        p2 = svc.record_payment(_pay(amount=600.0, payment_id="pay-mp-2"))
        svc.allocate(
            TENANT, p1.payment_id, "ord-mp", 400.0, capabilities=CAPS_ALLOC, idempotency_key="mp1"
        )
        svc.allocate(
            TENANT, p2.payment_id, "ord-mp", 600.0, capabilities=CAPS_ALLOC, idempotency_key="mp2"
        )
        self.assertEqual(svc.allocated_total_for_order(TENANT, "ord-mp"), 1000.0)
        unlock = svc.evaluate_fulfillment_unlock(TENANT, "ord-mp")
        self.assertEqual(unlock.code, UNLOCK_CONFIRMED)


# ---------------------------------------------------------------------------
# 6. Partial / overpayment
# ---------------------------------------------------------------------------


class PartialOverpaymentTests(unittest.TestCase):
    def test_allocate_under_exact_over_and_unlock_codes(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-po", amount=1000.0))

        under = svc.record_payment(_pay(amount=1000.0, payment_id="pay-under"))
        svc.allocate(
            TENANT,
            under.payment_id,
            "ord-po",
            400.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="po-under",
        )
        u_partial = svc.evaluate_fulfillment_unlock(TENANT, "ord-po")
        self.assertEqual(u_partial.code, UNLOCK_PARTIAL)

        # exact: allocate remaining 600 (supersedes prior for same payment+order)
        svc.allocate(
            TENANT,
            under.payment_id,
            "ord-po",
            1000.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="po-exact",
        )
        u_ok = svc.evaluate_fulfillment_unlock(TENANT, "ord-po")
        self.assertEqual(u_ok.code, UNLOCK_CONFIRMED)

        # overpay via second payment
        over = svc.record_payment(_pay(amount=200.0, payment_id="pay-over"))
        svc.allocate(
            TENANT,
            over.payment_id,
            "ord-po",
            200.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="po-over",
        )
        u_over = svc.evaluate_fulfillment_unlock(TENANT, "ord-po")
        self.assertEqual(u_over.code, UNLOCK_REVIEW)
        self.assertTrue(u_over.review_required)


# ---------------------------------------------------------------------------
# 7. Duplicate external id
# ---------------------------------------------------------------------------


class DuplicatePaymentTests(unittest.TestCase):
    def test_same_external_transaction_id_does_not_double_count(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-dup", amount=1000.0))
        first = svc.record_payment(
            _pay(
                payment_id="pay-dup-1",
                amount=1000.0,
                external_transaction_id="ext-dup-same",
            )
        )
        svc.allocate(
            TENANT,
            first.payment_id,
            "ord-dup",
            1000.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="dup-alloc-1",
        )
        self.assertEqual(svc.allocated_total_for_order(TENANT, "ord-dup"), 1000.0)

        again = svc.record_payment(
            _pay(
                payment_id="pay-dup-2",
                amount=1000.0,
                external_transaction_id="ext-dup-same",
            )
        )
        self.assertEqual(again.payment_id, first.payment_id)

        # Re-allocate with new idempotency key supersedes, still one active allocation sum
        svc.allocate(
            TENANT,
            again.payment_id,
            "ord-dup",
            1000.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="dup-alloc-2",
        )
        self.assertEqual(svc.allocated_total_for_order(TENANT, "ord-dup"), 1000.0)
        unlock = svc.evaluate_fulfillment_unlock(TENANT, "ord-dup")
        self.assertEqual(unlock.code, UNLOCK_CONFIRMED)
        self.assertEqual(len(unlock.payment_ids), 1)


# ---------------------------------------------------------------------------
# 8. Refund
# ---------------------------------------------------------------------------


class RefundTests(unittest.TestCase):
    def test_prepare_requires_cap(self):
        svc = _svc()
        pay = svc.record_payment(_pay(payment_id="pay-rf-1", amount=1000.0))
        with self.assertRaises(CapabilityDeniedError):
            svc.prepare_refund(TENANT, payment_id=pay.payment_id, amount=100.0, capabilities=())
        prep = svc.prepare_refund(
            TENANT,
            payment_id=pay.payment_id,
            amount=100.0,
            capabilities=CAPS_PREP,
            prepared_by="agent",
        )
        self.assertIn(prep.status, {REF_PREPARED, REF_AWAITING_APPROVAL})

    def test_execute_denied_without_execute_cap(self):
        svc = _svc()
        pay = svc.record_payment(_pay(payment_id="pay-rf-2", amount=1000.0))
        prep = svc.prepare_refund(
            TENANT,
            payment_id=pay.payment_id,
            amount=50.0,
            capabilities=CAPS_PREP,
        )
        with self.assertRaises(CapabilityDeniedError):
            svc.execute_refund(
                TENANT,
                refund_id=prep.refund_id,
                capabilities=CAPS_PREP,
                approval_id="appr-1",
            )

    def test_execute_denied_without_approval_id(self):
        svc = _svc()
        pay = svc.record_payment(_pay(payment_id="pay-rf-3", amount=1000.0))
        prep = svc.prepare_refund(
            TENANT,
            payment_id=pay.payment_id,
            amount=50.0,
            capabilities=CAPS_PREP,
        )
        with self.assertRaises(PolicyDeniedError):
            svc.execute_refund(
                TENANT,
                refund_id=prep.refund_id,
                capabilities=CAPS_EXEC,
                approval_id="",
            )

    def test_execute_with_caps_and_approval_succeeds(self):
        gw = FakePaymentGateway()
        svc = _svc(payment_gateway=gw)
        pay = svc.record_payment(
            _pay(
                payment_id="pay-rf-4",
                amount=1000.0,
                external_transaction_id="ext-rf-4",
            )
        )
        gw.seed_payment(pay)
        prep = svc.prepare_refund(
            TENANT,
            payment_id=pay.payment_id,
            amount=100.0,
            capabilities=CAPS_PREP,
        )
        done = svc.execute_refund(
            TENANT,
            refund_id=prep.refund_id,
            capabilities=CAPS_EXEC,
            approval_id="appr-ok",
            approved_by="reviewer",
            idempotency_key="exec-rf-4",
        )
        self.assertEqual(done.status, REF_CONFIRMED)

    def test_execute_idempotent(self):
        gw = FakePaymentGateway()
        svc = _svc(payment_gateway=gw)
        pay = svc.record_payment(
            _pay(
                payment_id="pay-rf-5",
                amount=1000.0,
                external_transaction_id="ext-rf-5",
            )
        )
        gw.seed_payment(pay)
        prep = svc.prepare_refund(
            TENANT,
            payment_id=pay.payment_id,
            amount=80.0,
            capabilities=CAPS_PREP,
            idempotency_key="prep-rf-5",
        )
        first = svc.execute_refund(
            TENANT,
            refund_id=prep.refund_id,
            capabilities=CAPS_EXEC,
            approval_id="appr-idemp",
            idempotency_key="exec-rf-5",
        )
        second = svc.execute_refund(
            TENANT,
            refund_id=prep.refund_id,
            capabilities=CAPS_EXEC,
            approval_id="appr-idemp",
            idempotency_key="exec-rf-5",
        )
        self.assertEqual(first.refund_id, second.refund_id)
        self.assertEqual(first.status, REF_CONFIRMED)
        self.assertEqual(second.status, REF_CONFIRMED)

    def test_force_timeout_on_refund_unknown_not_failed(self):
        gw = FakePaymentGateway()
        gw.force_timeout_on_refund = True
        svc = _svc(payment_gateway=gw)
        pay = svc.record_payment(
            _pay(
                payment_id="pay-rf-6",
                amount=1000.0,
                external_transaction_id="ext-rf-6",
            )
        )
        gw.seed_payment(pay)
        prep = svc.prepare_refund(
            TENANT,
            payment_id=pay.payment_id,
            amount=25.0,
            capabilities=CAPS_PREP,
        )
        result = svc.execute_refund(
            TENANT,
            refund_id=prep.refund_id,
            capabilities=CAPS_EXEC,
            approval_id="appr-to",
            idempotency_key="exec-rf-6",
        )
        self.assertEqual(result.status, REF_UNKNOWN_EXTERNAL)
        self.assertNotEqual(result.status, REF_FAILED)


# ---------------------------------------------------------------------------
# 9. Fulfillment unlock
# ---------------------------------------------------------------------------


class FulfillmentUnlockTests(unittest.TestCase):
    def test_unpaid_not_confirmed(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-unpaid", amount=500.0))
        unlock = svc.evaluate_fulfillment_unlock(TENANT, "ord-unpaid")
        self.assertEqual(unlock.code, UNLOCK_NOT_CONFIRMED)

    def test_paid_confirmed(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-paid", amount=500.0))
        pay = svc.record_payment(_pay(amount=500.0))
        svc.allocate(
            TENANT,
            pay.payment_id,
            "ord-paid",
            500.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="fu-paid",
        )
        unlock = svc.evaluate_fulfillment_unlock(TENANT, "ord-paid")
        self.assertEqual(unlock.code, UNLOCK_CONFIRMED)

    def test_review_required_path(self):
        svc = _svc()
        svc.register_order_target(
            TENANT, _target("ord-rev", amount=500.0, buyer_inn="7707083893")
        )
        pay = svc.record_payment(
            _pay(amount=500.0, payer_inn="1111111111")
        )
        svc.allocate(
            TENANT,
            pay.payment_id,
            "ord-rev",
            500.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="fu-rev",
        )
        unlock = svc.evaluate_fulfillment_unlock(TENANT, "ord-rev")
        self.assertEqual(unlock.code, UNLOCK_REVIEW)
        self.assertTrue(unlock.review_required)


# ---------------------------------------------------------------------------
# 10. Reconciliation
# ---------------------------------------------------------------------------


class ReconciliationTests(unittest.TestCase):
    def test_findings_order_paid_without_payment(self):
        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target("ord-ship", amount=1000.0, fulfillment_state="SHIPMENT"),
        )
        result = svc.reconcile_tenant(TENANT, workflow_id="wf-1", run_id="run-1")
        types = {f["finding_type"] for f in result["findings"]}
        self.assertIn("order_paid_without_payment", types)

    def test_payment_without_order(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-known", amount=100.0))
        svc.record_payment(_pay(amount=999.0, order_refs=()))
        # allocate to known so OK finding exists; orphan is the unallocated pay
        result = svc.reconcile_tenant(TENANT)
        types = {f["finding_type"] for f in result["findings"]}
        self.assertIn("payment_without_order", types)

    def test_amount_mismatch(self):
        svc = _svc()
        svc.register_order_target(TENANT, _target("ord-mm", amount=1000.0))
        # Keep PAY_PAID (no allocate) so reconcile compares payment amount vs order.
        svc.record_payment(
            _pay(amount=700.0, order_refs=("ord-mm",), payment_id="pay-mm")
        )
        result = svc.reconcile_tenant(TENANT)
        types = {f["finding_type"] for f in result["findings"]}
        self.assertTrue(
            {"payment_order_amount_mismatch", "partial_payment", "overpayment"}
            & types
        )

    def test_shipment_unpaid(self):
        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target("ord-ship2", amount=2000.0, shipment_started=True),
        )
        result = svc.reconcile_tenant(TENANT)
        types = {f["finding_type"] for f in result["findings"]}
        self.assertIn("shipment_without_confirmed_payment", types)

    def test_payer_mismatch(self):
        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target("ord-pm", amount=500.0, buyer_inn="7707083893"),
        )
        pay = svc.record_payment(
            _pay(
                amount=500.0,
                order_refs=("ord-pm",),
                payer_inn="0000000000",
                payment_id="pay-pm",
            )
        )
        svc.allocate(
            TENANT,
            pay.payment_id,
            "ord-pm",
            500.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="pm-alloc",
        )
        result = svc.reconcile_tenant(TENANT)
        types = {f["finding_type"] for f in result["findings"]}
        self.assertIn("payer_legal_entity_mismatch", types)

    def test_chargeback_after_fulfillment(self):
        from payments.states import PAY_CHARGEBACK

        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target("ord-cb", amount=500.0, shipment_started=True),
        )
        svc.record_payment(
            _pay(
                amount=500.0,
                order_refs=("ord-cb",),
                status=PAY_CHARGEBACK,
                payment_id="pay-cb",
            )
        )
        result = svc.reconcile_tenant(TENANT)
        types = {f["finding_type"] for f in result["findings"]}
        self.assertIn("chargeback_after_fulfillment", types)

    def test_marking_incomplete(self):
        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target("ord-mk", amount=500.0, marking_incomplete=True),
        )
        pay = svc.record_payment(
            _pay(amount=500.0, order_refs=("ord-mk",), payment_id="pay-mk")
        )
        svc.allocate(
            TENANT,
            pay.payment_id,
            "ord-mk",
            500.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="mk-alloc",
        )
        result = svc.reconcile_tenant(TENANT)
        types = {f["finding_type"] for f in result["findings"]}
        self.assertIn("payment_marking_incomplete", types)

    def test_persist_findings_and_cross_tenant_inaccessible(self):
        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target("ord-pers", amount=1000.0, fulfillment_state="SHIPMENT"),
        )
        result = svc.reconcile_tenant(TENANT, run_id="run-pers")
        self.assertTrue(result["findings"])
        fid = result["findings"][0]["finding_id"]
        row = svc.store.get_finding(TENANT, fid)
        self.assertIsNotNone(row)
        self.assertEqual(row["finding_id"], fid)
        self.assertEqual(svc.store.list_findings("tenant-b"), [])
        self.assertIsNone(svc.store.get_finding("tenant-b", fid))


# ---------------------------------------------------------------------------
# 11. Persistence restart
# ---------------------------------------------------------------------------


class PersistenceRestartTests(unittest.TestCase):
    def test_save_reopen_load_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "payments.sqlite3")
            store1 = PaymentsStore(path=path)
            svc1 = _svc(store=store1)
            pay = svc1.record_payment(
                _pay(
                    payment_id="pay-persist-1",
                    amount=1234.0,
                    external_transaction_id="ext-persist-1",
                    order_refs=("ord-p",),
                )
            )
            store1.close()

            store2 = PaymentsStore(path=path)
            loaded = store2.get_payment(TENANT, pay.payment_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["payment_id"], "pay-persist-1")
            self.assertEqual(float(loaded["amount"]), 1234.0)
            by_ext = store2.get_payment_by_external(TENANT, "ext-persist-1")
            self.assertIsNotNone(by_ext)
            self.assertEqual(by_ext["payment_id"], "pay-persist-1")
            store2.close()


# ---------------------------------------------------------------------------
# 12. Scheduler / production
# ---------------------------------------------------------------------------


class SchedulerProductionTests(unittest.IsolatedAsyncioTestCase):
    def test_compose_payments_runtime_and_shared_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pay-sched.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env=_env(
                    path,
                    PAYMENTS_RECONCILIATION_ENABLED="true",
                    PAYMENTS_RECONCILIATION_INTERVAL_SECONDS="120",
                    PAYMENTS_RECONCILIATION_TENANTS="tenant-a,tenant-b",
                ),
            )
            try:
                pr = runtime.payments_runtime
                self.assertIsNotNone(pr)
                self.assertTrue(pr.reconciliation_enabled)
                self.assertEqual(pr.reconciliation_interval_seconds, 120.0)
                self.assertIs(
                    pr.reconciliation_scheduler.scheduler,
                    runtime.workflow_runtime.scheduler,
                )
                ids = {
                    s.schedule_id
                    for s in runtime.workflow_runtime.scheduler.store.list_all()
                    if s.workflow_type == "payments.reconcile"
                }
                self.assertEqual(
                    ids,
                    {"payments-reconcile:tenant-a", "payments-reconcile:tenant-b"},
                )
            finally:
                runtime.close()

    async def test_tick_creates_payments_reconcile_idempotent_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pay-tick.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env=_env(
                    path,
                    PAYMENTS_RECONCILIATION_ENABLED="true",
                    PAYMENTS_RECONCILIATION_INTERVAL_SECONDS="3600",
                    PAYMENTS_RECONCILIATION_TENANTS="tenant-a,tenant-b",
                ),
            )
            try:
                wr = runtime.workflow_runtime
                now = utc_now()
                for sid in (
                    "payments-reconcile:tenant-a",
                    "payments-reconcile:tenant-b",
                ):
                    st = wr.scheduler.store.get(sid)
                    wr.scheduler.store.save(
                        replace(st, next_run_at=now - timedelta(seconds=1))
                    )

                launched = await wr.tick_schedules()
                self.assertEqual(len(launched), 2)
                states = [wr.state_manager.get(wid) for wid in launched]
                tenants = {dict(s.metadata).get("tenant_id") for s in states}
                self.assertEqual(tenants, {"tenant-a", "tenant-b"})
                for s in states:
                    self.assertTrue(
                        str(s.execution_key).startswith("payments-reconcile:")
                    )

                keys_before = {s.execution_key for s in states}
                for s in states:
                    tenant = dict(s.metadata).get("tenant_id")
                    sid = f"payments-reconcile:{tenant}"
                    window = int(str(s.execution_key).rsplit(":", 1)[-1])
                    st = wr.scheduler.store.get(sid)
                    wr.scheduler.store.save(
                        replace(
                            st,
                            next_run_at=datetime.fromtimestamp(
                                window, tz=timezone.utc
                            ),
                        )
                    )

                launched2 = await wr.tick_schedules()
                self.assertEqual(len(launched2), 2)
                self.assertEqual(set(launched2), set(launched))
                self.assertEqual(
                    {wr.state_manager.get(w).execution_key for w in launched2},
                    keys_before,
                )
            finally:
                runtime.close()


# ---------------------------------------------------------------------------
# 13. Tools
# ---------------------------------------------------------------------------


class ToolsTests(unittest.TestCase):
    def test_tool_registry_payments_read_and_execute_refund_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pay-tools.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env=_env(path),
            )
            try:
                self.assertIsNotNone(runtime.payments_runtime)
                for tool_id in ("payments.read", "payments.execute_refund"):
                    desc = runtime.tool_registry.get(tool_id)
                    self.assertTrue(desc.enabled, tool_id)
            finally:
                runtime.close()


# ---------------------------------------------------------------------------
# 14. Commerce linkage
# ---------------------------------------------------------------------------


class CommerceLinkageTests(unittest.TestCase):
    def test_apply_unlock_updates_commerce_payment_reference(self):
        commerce = CommerceService(store=CommerceStore(path=":memory:"))
        order = CommerceOrder(
            order_id="ord-link-1",
            tenant_id=TENANT,
            buyer_type="B2C",
            buyer_ref="buyer-1",
            lines=(
                CommerceOrderLine(
                    product_ref="sku-1",
                    quantity=1,
                    warehouse="main",
                ),
            ),
            fulfillment_state=ORDER_NEW,
            payment_status="unconfirmed",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        commerce.store.save_order(
            TENANT, order.order_id, _order_payload(order), ORDER_NEW
        )

        svc = _svc(commerce_service=commerce)
        svc.register_order_target(TENANT, _target("ord-link-1", amount=100.0))
        pay = svc.record_payment(_pay(amount=100.0, payment_id="pay-link-1"))
        svc.allocate(
            TENANT,
            pay.payment_id,
            "ord-link-1",
            100.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="link-alloc",
        )
        unlock = svc.apply_unlock_to_commerce(TENANT, "ord-link-1")
        self.assertEqual(unlock.code, UNLOCK_CONFIRMED)
        loaded = commerce._get_order(TENANT, "ord-link-1")
        self.assertEqual(loaded.payment_status, "confirmed")
        self.assertEqual(
            dict(loaded.provenance).get("payment_unlock_code"), UNLOCK_CONFIRMED
        )
        self.assertIn("pay-link-1", dict(loaded.provenance).get("payment_refs") or [])

    def test_fake_commerce_capture(self):
        fake = FakeCommerceUnlock()
        svc = _svc(commerce_service=fake)
        svc.register_order_target(TENANT, _target("ord-fake", amount=50.0))
        pay = svc.record_payment(_pay(amount=50.0))
        svc.allocate(
            TENANT,
            pay.payment_id,
            "ord-fake",
            50.0,
            capabilities=CAPS_ALLOC,
            idempotency_key="fake-alloc",
        )
        svc.apply_unlock_to_commerce(TENANT, "ord-fake")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["payment_status"], "confirmed")
        self.assertEqual(fake.calls[0]["unlock_code"], UNLOCK_CONFIRMED)


# ---------------------------------------------------------------------------
# 15. No automatic fiscal/marking correction
# ---------------------------------------------------------------------------


class NoAutoCorrectTests(unittest.TestCase):
    def test_reconcile_auto_corrected_false_in_evidence(self):
        svc = _svc()
        svc.register_order_target(
            TENANT,
            _target(
                "ord-ac",
                amount=1000.0,
                fulfillment_state="SHIPMENT",
                marking_incomplete=True,
            ),
        )
        result = svc.reconcile_tenant(TENANT, run_id="run-ac")
        self.assertFalse(result["auto_corrected"])
        for finding in result["findings"]:
            evidence = finding.get("evidence") or {}
            self.assertFalse(evidence.get("auto_corrected", True))
            persisted = svc.store.get_finding(TENANT, finding["finding_id"])
            self.assertIsNotNone(persisted)
            self.assertFalse(
                (persisted.get("evidence") or {}).get("auto_corrected", True)
            )


# ---------------------------------------------------------------------------
# Smoke: evals
# ---------------------------------------------------------------------------


class PaymentEvalsSmokeTests(unittest.TestCase):
    def test_run_payment_eval_cases_smoke(self):
        cases = run_payment_eval_cases()
        self.assertTrue(cases)
        for name, ok in cases:
            self.assertTrue(ok, msg=f"eval case failed: {name}")


if __name__ == "__main__":
    unittest.main()
