"""Deterministic payment-to-order / B2B matching — evidence + confidence."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from payments.contracts import (
    BankTransaction,
    OrderPaymentTarget,
    PaymentMatchResult,
    PaymentRecord,
)
from payments.policy import PaymentPolicy, PaymentPolicyEngine
from payments.states import ALLOC_REVIEW

_INVOICE_RE = re.compile(
    r"(?:inv|invoice|сч[её]т)[^\d]{0,6}(\d[\w\-/]*)", re.IGNORECASE
)
_ORDER_RE = re.compile(r"(?:order|заказ)[^\d]{0,6}(\d[\w\-/]*)", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _extract_refs(purpose: str) -> dict:
    inv = _INVOICE_RE.search(purpose or "")
    ord_ = _ORDER_RE.search(purpose or "")
    return {
        "invoice": inv.group(1) if inv else "",
        "order": ord_.group(1) if ord_ else "",
    }


class PaymentMatcher:
    def __init__(self, policy_engine: PaymentPolicyEngine | None = None):
        self.policies = policy_engine or PaymentPolicyEngine()

    def match_payment(
        self,
        payment: PaymentRecord,
        targets: list[OrderPaymentTarget],
        *,
        policy: PaymentPolicy | None = None,
    ) -> PaymentMatchResult:
        pol = policy or self.policies.active()
        return self._match(
            tenant_id=payment.tenant_id,
            payment_id=payment.payment_id,
            bank_transaction_id="",
            amount=payment.amount,
            currency=payment.currency,
            order_refs=payment.order_refs,
            invoice_refs=payment.invoice_refs,
            payer_inn=payment.payer_inn,
            payer_name=payment.payer_name,
            purpose=" ".join(payment.invoice_refs + payment.order_refs),
            occurred_at=payment.occurred_at,
            unique_ref=payment.external_transaction_id,
            targets=targets,
            policy=pol,
        )

    def match_bank_transaction(
        self,
        tx: BankTransaction,
        targets: list[OrderPaymentTarget],
        *,
        policy: PaymentPolicy | None = None,
    ) -> PaymentMatchResult:
        pol = policy or self.policies.active()
        refs = _extract_refs(tx.purpose)
        invoice_refs = tuple(
            x for x in (tx.invoice_ref, refs.get("invoice") or "") if x
        )
        order_refs = tuple(x for x in (tx.order_ref, refs.get("order") or "") if x)
        return self._match(
            tenant_id=tx.tenant_id,
            payment_id="",
            bank_transaction_id=tx.transaction_id,
            amount=tx.amount,
            currency=tx.currency,
            order_refs=order_refs,
            invoice_refs=invoice_refs,
            payer_inn=tx.payer_inn,
            payer_name=tx.payer_name,
            purpose=tx.purpose,
            occurred_at=tx.booked_at or tx.value_date,
            unique_ref=tx.document_ref or tx.external_bank_id,
            targets=targets,
            policy=pol,
        )

    def _match(
        self,
        *,
        tenant_id: str,
        payment_id: str,
        bank_transaction_id: str,
        amount: float,
        currency: str,
        order_refs: tuple[str, ...],
        invoice_refs: tuple[str, ...],
        payer_inn: str,
        payer_name: str,
        purpose: str,
        occurred_at: datetime | None,
        unique_ref: str,
        targets: list[OrderPaymentTarget],
        policy: PaymentPolicy,
    ) -> PaymentMatchResult:
        candidates: list[tuple[OrderPaymentTarget, float, dict, list[str]]] = []
        purpose_refs = _extract_refs(purpose)
        for t in targets:
            if t.tenant_id != tenant_id:
                continue
            if policy.currency_strict and t.currency != currency:
                continue
            score = 0.0
            evidence: dict = {}
            conflicts: list[str] = []

            # 1. explicit order/payment reference
            if order_refs and t.order_id in order_refs:
                score += 100
                evidence["explicit_order_ref"] = t.order_id
            if unique_ref and t.payment_reference and unique_ref == t.payment_reference:
                score += 95
                evidence["unique_payment_reference"] = unique_ref

            # 2. invoice number
            inv_candidates = set(invoice_refs) | {
                purpose_refs.get("invoice") or ""
            }
            inv_candidates.discard("")
            if t.invoice_number and t.invoice_number in inv_candidates:
                score += 90
                evidence["invoice_number"] = t.invoice_number
            if t.invoice_number and t.invoice_number in (purpose or ""):
                score += 70
                evidence["invoice_in_purpose"] = t.invoice_number

            # 3 already covered unique ref

            # 4. exact amount + counterparty
            amount_ok = self.policies.within_tolerance(t.amount, amount, policy)
            if amount_ok:
                score += 40
                evidence["amount"] = amount
            else:
                evidence["amount_delta"] = abs(t.amount - amount)

            # 5. INN
            if payer_inn and t.buyer_inn:
                if payer_inn == t.buyer_inn:
                    score += 50
                    evidence["inn"] = payer_inn
                else:
                    conflicts.append("inn_mismatch")
                    score -= 80
                    evidence["inn_expected"] = t.buyer_inn
                    evidence["inn_actual"] = payer_inn

            # 6. payment purpose order
            if purpose_refs.get("order") == t.order_id or t.order_id in (purpose or ""):
                score += 55
                evidence["order_in_purpose"] = t.order_id

            # 7. date window
            if occurred_at is not None:
                # targets don't carry created_at — treat as soft signal when present in evidence only
                evidence["occurred_at"] = occurred_at.isoformat()
                score += 5

            # 8. company name fallback (weak)
            if payer_name and t.buyer_name:
                if _norm(payer_name) == _norm(t.buyer_name):
                    score += 15
                    evidence["payer_name"] = payer_name
                elif _norm(payer_name) in _norm(t.buyer_name) or _norm(
                    t.buyer_name
                ) in _norm(payer_name):
                    score += 8
                    evidence["payer_name_partial"] = payer_name
                else:
                    evidence["payer_name_mismatch"] = True

            # Strong identifier conflicts block auto-match
            if "inn_mismatch" in conflicts and order_refs and t.order_id in order_refs:
                # explicit order + wrong INN → conflict, still candidate for review
                pass

            if score > 0 or conflicts:
                candidates.append((t, score, evidence, conflicts))

        if not candidates:
            return PaymentMatchResult(
                match_id=f"match-{uuid.uuid4().hex[:10]}",
                tenant_id=tenant_id,
                payment_id=payment_id,
                bank_transaction_id=bank_transaction_id,
                review_required=True,
                status=ALLOC_REVIEW,
                confidence=0.0,
                evidence={"reason": "no_candidates"},
            )

        candidates.sort(key=lambda x: x[1], reverse=True)
        best_t, best_score, best_ev, best_conf = candidates[0]
        # Normalize confidence 0..1
        confidence = min(1.0, max(0.0, best_score / 100.0))
        # Ambiguous: close second score
        ambiguous = False
        if len(candidates) > 1 and abs(candidates[0][1] - candidates[1][1]) < 10:
            ambiguous = True
            best_conf = list(best_conf) + ["ambiguous_candidates"]

        # Conflict on strong IDs
        if "inn_mismatch" in best_conf:
            review = True
            status = ALLOC_REVIEW
            selected_order = ""
            selected_invoice = ""
        elif ambiguous or confidence < policy.auto_match_confidence_threshold:
            review = True
            status = ALLOC_REVIEW
            selected_order = best_t.order_id if confidence >= 0.5 else ""
            selected_invoice = best_t.invoice_number if confidence >= 0.5 else ""
        else:
            review = False
            status = "MATCHED"
            selected_order = best_t.order_id
            selected_invoice = best_t.invoice_number

        return PaymentMatchResult(
            match_id=f"match-{uuid.uuid4().hex[:10]}",
            tenant_id=tenant_id,
            payment_id=payment_id,
            bank_transaction_id=bank_transaction_id,
            candidate_order_refs=tuple(c[0].order_id for c in candidates[:5]),
            candidate_invoice_refs=tuple(
                c[0].invoice_number for c in candidates[:5] if c[0].invoice_number
            ),
            selected_order_id=selected_order,
            selected_invoice_id=selected_invoice,
            evidence=best_ev,
            conflicts=tuple(best_conf),
            confidence=confidence,
            review_required=review,
            status=status,
        )
