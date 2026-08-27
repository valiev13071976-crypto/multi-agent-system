"""Payments + cross-system reconciliation findings (no auto-correction)."""

from __future__ import annotations

import uuid
from typing import Mapping

from payments.contracts import (
    OrderPaymentTarget,
    PaymentRecord,
    ReconciliationFinding,
)
from payments.policy import PaymentPolicy, PaymentPolicyEngine
from payments.states import (
    PAY_CHARGEBACK,
    PAY_DISPUTED,
    PAY_FAILED,
    PAY_PAID,
    PAY_CAPTURED,
    PAY_REFUNDED,
)


def _fid() -> str:
    return f"pf-{uuid.uuid4().hex[:10]}"


class PaymentsReconciliationEngine:
    def __init__(self, policy_engine: PaymentPolicyEngine | None = None):
        self.policies = policy_engine or PaymentPolicyEngine()

    def reconcile_order(
        self,
        *,
        tenant_id: str,
        target: OrderPaymentTarget,
        payments: list[PaymentRecord],
        allocations: list[dict],
        bank_txs: list[dict],
        refunds: list[dict],
        workflow_ref: str = "",
        policy: PaymentPolicy | None = None,
    ) -> list[ReconciliationFinding]:
        pol = policy or self.policies.active()
        findings: list[ReconciliationFinding] = []
        allocated = sum(
            float(a.get("allocated_amount") or 0)
            for a in allocations
            if a.get("status") != "SUPERSEDED" and a.get("order_id") == target.order_id
        )
        confirmed_pays = [
            p
            for p in payments
            if p.status in {PAY_PAID, PAY_CAPTURED}
            and (
                target.order_id in p.order_refs
                or any(a.get("payment_id") == p.payment_id and a.get("order_id") == target.order_id for a in allocations)
            )
        ]

        # order marked conceptually paid via allocations vs no payment
        order_claims_paid = allocated + 1e-9 >= target.amount or target.fulfillment_state in {
            "PAID",
            "RESERVED",
            "FULFILLMENT",
            "SHIPMENT",
            "COMPLETED",
        }

        if order_claims_paid and not confirmed_pays and allocated < target.amount - pol.amount_tolerance:
            if target.fulfillment_state in {"PAID", "SHIPMENT", "COMPLETED", "FULFILLMENT"}:
                findings.append(
                    self._finding(
                        tenant_id,
                        "order_paid_without_payment",
                        "RECONCILIATION_ERROR",
                        "HUMAN_REVIEW",
                        refs={"order_id": target.order_id},
                        expected={"amount": target.amount},
                        actual={"allocated": allocated, "payments": 0},
                        workflow_ref=workflow_ref,
                    )
                )

        # payment without order (checked at tenant level separately)

        if confirmed_pays:
            total = sum(p.amount for p in confirmed_pays)
            # prefer allocation sum when present
            compare = allocated if allocations else total
            if abs(compare - target.amount) > pol.amount_tolerance:
                severity = "WARNING"
                status = "WARNING"
                ftype = "payment_order_amount_mismatch"
                if compare < target.amount - pol.amount_tolerance:
                    ftype = "partial_payment"
                elif compare > target.amount + pol.amount_tolerance:
                    ftype = "overpayment"
                    status = "PAYMENT_REVIEW_REQUIRED"
                findings.append(
                    self._finding(
                        tenant_id,
                        ftype,
                        severity if ftype != "overpayment" else "WARNING",
                        status,
                        refs={"order_id": target.order_id},
                        expected={"amount": target.amount},
                        actual={"allocated": compare},
                        workflow_ref=workflow_ref,
                    )
                )

        # fiscal
        if confirmed_pays and not target.fiscal_receipt_ref:
            findings.append(
                self._finding(
                    tenant_id,
                    "payment_without_fiscal_receipt",
                    "WARNING",
                    "WARNING",
                    refs={"order_id": target.order_id},
                    expected={"receipt": True},
                    actual={"receipt": False},
                    workflow_ref=workflow_ref,
                )
            )
        if target.fiscal_receipt_ref and target.fiscal_amount is not None and confirmed_pays:
            pay_amt = sum(p.amount for p in confirmed_pays)
            if abs(float(target.fiscal_amount) - pay_amt) > pol.amount_tolerance:
                findings.append(
                    self._finding(
                        tenant_id,
                        "fiscal_amount_mismatch",
                        "RECONCILIATION_ERROR",
                        "HUMAN_REVIEW",
                        refs={"order_id": target.order_id, "receipt": target.fiscal_receipt_ref},
                        expected={"payment": pay_amt},
                        actual={"fiscal": target.fiscal_amount},
                        workflow_ref=workflow_ref,
                    )
                )

        # shipment without payment
        if target.shipment_started and allocated < target.amount - pol.amount_tolerance:
            findings.append(
                self._finding(
                    tenant_id,
                    "shipment_without_confirmed_payment",
                    "RECONCILIATION_ERROR",
                    "HUMAN_REVIEW",
                    refs={"order_id": target.order_id},
                    expected={"allocated": target.amount},
                    actual={"allocated": allocated},
                    workflow_ref=workflow_ref,
                )
            )

        # paid order cancelled
        if target.cancelled and (confirmed_pays or allocated > 0):
            findings.append(
                self._finding(
                    tenant_id,
                    "paid_order_cancelled",
                    "WARNING",
                    "PAYMENT_REVIEW_REQUIRED",
                    refs={"order_id": target.order_id},
                    expected={"active": True},
                    actual={"cancelled": True},
                    workflow_ref=workflow_ref,
                )
            )

        # marking incomplete after payment
        if confirmed_pays and target.marking_incomplete:
            findings.append(
                self._finding(
                    tenant_id,
                    "payment_marking_incomplete",
                    "WARNING",
                    "WARNING",
                    refs={"order_id": target.order_id},
                    expected={"marking_complete": True},
                    actual={"marking_incomplete": True},
                    workflow_ref=workflow_ref,
                )
            )

        # payer mismatch
        for p in confirmed_pays:
            if p.payer_inn and target.buyer_inn and p.payer_inn != target.buyer_inn:
                findings.append(
                    self._finding(
                        tenant_id,
                        "payer_legal_entity_mismatch",
                        "WARNING",
                        "PAYMENT_REVIEW_REQUIRED",
                        refs={"order_id": target.order_id, "payment_id": p.payment_id},
                        expected={"inn": target.buyer_inn},
                        actual={"inn": p.payer_inn},
                        workflow_ref=workflow_ref,
                    )
                )

        # chargeback after fulfillment
        for p in payments:
            if p.status in {PAY_CHARGEBACK, PAY_DISPUTED} and target.shipment_started:
                findings.append(
                    self._finding(
                        tenant_id,
                        "chargeback_after_fulfillment",
                        "RECONCILIATION_ERROR",
                        "HUMAN_REVIEW",
                        refs={"order_id": target.order_id, "payment_id": p.payment_id},
                        expected={"status": PAY_PAID},
                        actual={"status": p.status},
                        workflow_ref=workflow_ref,
                    )
                )

        # refund only in one system (local refund vs payment.refunded)
        local_refund_amt = sum(
            float(r.get("amount") or 0)
            for r in refunds
            if r.get("status") in {"CONFIRMED", "SUBMITTED", "PARTIAL"}
        )
        gateway_refunded = sum(p.refunded_amount for p in payments) + sum(
            p.amount for p in payments if p.status == PAY_REFUNDED
        )
        if local_refund_amt > 0 and gateway_refunded <= 0 and not any(
            p.status == PAY_REFUNDED for p in payments
        ):
            findings.append(
                self._finding(
                    tenant_id,
                    "refund_only_local",
                    "RECONCILIATION_ERROR",
                    "HUMAN_REVIEW",
                    refs={"order_id": target.order_id},
                    expected={"gateway_refund": True},
                    actual={"local_refund": local_refund_amt},
                    workflow_ref=workflow_ref,
                )
            )

        # bank presence soft check
        if confirmed_pays and not bank_txs and any(p.source == "gateway" for p in confirmed_pays):
            # only warn when policy expects bank confirmation — optional soft
            pass

        if not findings:
            findings.append(
                self._finding(
                    tenant_id,
                    "ok",
                    "OK",
                    "OK",
                    refs={"order_id": target.order_id},
                    expected={},
                    actual={"allocated": allocated},
                    workflow_ref=workflow_ref,
                )
            )
        return findings

    def detect_orphan_payments(
        self,
        *,
        tenant_id: str,
        payments: list[PaymentRecord],
        allocations: list[dict],
        known_orders: set[str],
        workflow_ref: str = "",
    ) -> list[ReconciliationFinding]:
        out = []
        allocated_payments = {a.get("payment_id") for a in allocations if a.get("status") != "SUPERSEDED"}
        for p in payments:
            if p.status not in {PAY_PAID, PAY_CAPTURED}:
                continue
            linked = set(p.order_refs) & known_orders
            if not linked and p.payment_id not in allocated_payments:
                out.append(
                    self._finding(
                        tenant_id,
                        "payment_without_order",
                        "WARNING",
                        "PAYMENT_REVIEW_REQUIRED",
                        refs={"payment_id": p.payment_id},
                        expected={"order": True},
                        actual={"order_refs": list(p.order_refs)},
                        workflow_ref=workflow_ref,
                    )
                )
        # duplicates by external id already prevented at store — soft duplicate amount/date
        seen: dict[tuple, str] = {}
        for p in payments:
            key = (p.external_transaction_id or "", round(p.amount, 2), p.currency)
            if p.external_transaction_id and key in seen and seen[key] != p.payment_id:
                out.append(
                    self._finding(
                        tenant_id,
                        "duplicate_payment",
                        "RECONCILIATION_ERROR",
                        "HUMAN_REVIEW",
                        refs={"payment_id": p.payment_id, "other": seen[key]},
                        expected={"unique": True},
                        actual={"external_transaction_id": p.external_transaction_id},
                        workflow_ref=workflow_ref,
                    )
                )
            elif p.external_transaction_id:
                seen[key] = p.payment_id
        return out

    def _finding(
        self,
        tenant_id: str,
        finding_type: str,
        severity: str,
        status: str,
        *,
        refs: Mapping,
        expected: Mapping,
        actual: Mapping,
        workflow_ref: str,
    ) -> ReconciliationFinding:
        return ReconciliationFinding(
            finding_id=_fid(),
            tenant_id=tenant_id,
            finding_type=finding_type,
            severity=severity,
            status=status,
            refs=dict(refs),
            expected=dict(expected),
            actual=dict(actual),
            evidence={"auto_corrected": False},
            workflow_ref=workflow_ref,
        )
