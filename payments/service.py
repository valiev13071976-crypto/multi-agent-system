"""Payments orchestration service — matching, allocation, unlock, refunds, reconcile.

Never processes card data. Never mutates commerce inline from webhooks.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from autonomy.models import (
    ACTION_FINANCIAL_CHANGE,
    ACTION_WRITE,
    DECISION_REQUIRE_APPROVAL,
    RISK_HIGH,
    AutonomyDecision,
    ProposedAction,
    utc_now,
)
from payments.capabilities import (
    CAP_PAYMENTS_ALLOCATE,
    CAP_PAYMENTS_EXECUTE_REFUND,
    CAP_PAYMENTS_PREPARE_REFUND,
    LLM_DEFAULT_DENY,
)
from payments.contracts import (
    BankTransaction,
    FulfillmentUnlockResult,
    OrderPaymentTarget,
    PaymentAllocation,
    PaymentMatchResult,
    PaymentRecord,
    RefundRecord,
    assert_no_card_data,
)
from payments.errors import (
    CapabilityDeniedError,
    CurrencyMismatchError,
    ExternalUnconfirmedError,
    PolicyDeniedError,
    RefundNotPreparedError,
    TenantAccessDeniedError,
)
from payments.matcher import PaymentMatcher
from payments.normalize import (
    event_to_payment_status,
    extract_safe_payment_fields,
    normalize_event_type,
)
from payments.policy import PaymentPolicyEngine
from payments.reconcile import PaymentsReconciliationEngine
from payments.states import (
    ALLOC_CONFIRMED,
    ALLOC_SUPERSEDED,
    PAY_CREATED,
    PAY_OVERPAID,
    PAY_PAID,
    PAY_PARTIALLY_PAID,
    PAY_PARTIALLY_REFUNDED,
    PAY_RECONCILIATION_REQUIRED,
    PAY_REFUND_PENDING,
    PAY_REFUNDED,
    PAY_UNDERPAID,
    REF_AWAITING_APPROVAL,
    REF_CONFIRMED,
    REF_FAILED,
    REF_PARTIAL,
    REF_PREPARED,
    REF_REQUESTED,
    REF_SUBMITTED,
    REF_UNKNOWN_EXTERNAL,
    UNLOCK_BLOCKED,
    UNLOCK_CONFIRMED,
    UNLOCK_NOT_CONFIRMED,
    UNLOCK_PARTIAL,
    UNLOCK_REVIEW,
    assert_transition,
)
from payments.store import PaymentsStore
from security.tenant import normalize_tenant_id


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class PaymentsService:
    def __init__(
        self,
        *,
        store: PaymentsStore,
        payment_gateway,
        bank_gateway,
        policy_engine: PaymentPolicyEngine | None = None,
        matcher: PaymentMatcher | None = None,
        recon_engine: PaymentsReconciliationEngine | None = None,
        workflow_runtime=None,
        hitl=None,
        commerce_service=None,
        integration_webhooks=None,
        data_intelligence=None,
        observability=None,
        bitrix_bridge=None,
    ):
        self.store = store
        self.payment_gateway = payment_gateway
        self.bank_gateway = bank_gateway
        self.policy_engine = policy_engine or PaymentPolicyEngine()
        self.matcher = matcher or PaymentMatcher(self.policy_engine)
        self.recon_engine = recon_engine or PaymentsReconciliationEngine(self.policy_engine)
        self.workflow_runtime = workflow_runtime
        self.hitl = hitl
        self.commerce_service = commerce_service
        self.integration_webhooks = integration_webhooks
        self.data_intelligence = data_intelligence
        self.observability = observability
        self.bitrix_bridge = bitrix_bridge
        self.metrics: dict[str, int] = {
            "webhooks_processed": 0,
            "webhooks_duplicate": 0,
            "payments_recorded": 0,
            "duplicate_payments_detected": 0,
            "bank_tx_ingested": 0,
            "bank_tx_deduped": 0,
            "matches_created": 0,
            "matches_review": 0,
            "allocations_created": 0,
            "refunds_prepared": 0,
            "refunds_executed": 0,
            "refunds_unknown_external": 0,
            "reconcile_runs": 0,
            "findings_human_review": 0,
            "unlock_confirmed": 0,
            "unlock_blocked": 0,
            "workflows_enqueued": 0,
        }

    # ---- helpers ----

    def _inc(self, name: str, n: int = 1) -> None:
        self.metrics[name] = int(self.metrics.get(name) or 0) + n
        obs = self.observability
        if obs is not None and hasattr(obs, "increment"):
            try:
                obs.increment(f"payments.{name}", n)
            except Exception:
                pass

    def _require_cap(self, capabilities: tuple[str, ...] | list[str] | None, needed: str) -> None:
        held = set(capabilities or ())
        if needed not in held:
            raise CapabilityDeniedError("capability_denied")
        if needed in LLM_DEFAULT_DENY and needed not in held:
            raise CapabilityDeniedError("capability_denied")

    def _request_hitl(
        self,
        *,
        tenant_id: str,
        action_id: str,
        workflow_id: str,
        task_id: str,
        operation: str,
        resource: str,
        reason_code: str,
        capabilities_checked: tuple[str, ...] = (),
        metadata: Mapping[str, object] | None = None,
        requested_by: str = "payments",
    ) -> str | None:
        if self.hitl is None:
            self.store.audit(
                tenant_id,
                "hitl_skipped",
                refs={"resource": resource},
                details={"operation": operation, "reason_code": reason_code},
            )
            return None
        meta = dict(metadata or {})
        action = ProposedAction(
            action_id=action_id,
            workflow_id=workflow_id,
            task_id=task_id,
            action_type=ACTION_FINANCIAL_CHANGE if "refund" in operation else ACTION_WRITE,
            tool_id="payments.service",
            operation=operation,
            resource=resource,
            risk_class=RISK_HIGH,
            requested_capabilities=capabilities_checked,
            tool_trust_level="PRIVILEGED",
            idempotency_key=meta.get("idempotency_key") if isinstance(meta.get("idempotency_key"), str) else None,
            metadata=meta,
        )
        decision = AutonomyDecision(
            decision_id=f"dec-{action_id}",
            action_id=action.action_id,
            decision=DECISION_REQUIRE_APPROVAL,
            risk_class=RISK_HIGH,
            reason_code=reason_code,
            required_approval=True,
            capabilities_checked=tuple(capabilities_checked),
            idempotency_required=bool(action.idempotency_key),
            idempotency_satisfied=True,
            tool_trust_level="PRIVILEGED",
            timestamp=utc_now(),
            metadata={"reason_code": reason_code, **meta},
        )
        record = self.hitl.request_approval(action, decision, requested_by=requested_by)
        approval_id = getattr(record, "approval_id", None)
        self.store.audit(
            tenant_id,
            "hitl_requested",
            refs={"resource": resource, "approval_id": approval_id or ""},
            details={"operation": operation, "reason_code": reason_code},
        )
        return approval_id

    @staticmethod
    def _payment_to_dict(payment: PaymentRecord) -> dict:
        return {
            "payment_id": payment.payment_id,
            "tenant_id": payment.tenant_id,
            "provider": payment.provider,
            "amount": float(payment.amount),
            "currency": payment.currency,
            "status": payment.status,
            "payment_method_type": payment.payment_method_type,
            "external_transaction_id": payment.external_transaction_id,
            "order_refs": list(payment.order_refs),
            "invoice_refs": list(payment.invoice_refs),
            "payer_ref": payment.payer_ref,
            "payer_inn": payment.payer_inn,
            "payer_name": payment.payer_name,
            "authorized_amount": float(payment.authorized_amount),
            "captured_amount": float(payment.captured_amount),
            "refunded_amount": float(payment.refunded_amount),
            "occurred_at": _iso(payment.occurred_at),
            "received_at": _iso(payment.received_at) or _utc().isoformat(),
            "source": payment.source,
            "provenance": dict(payment.provenance),
            "metadata": dict(payment.metadata),
            "version": int(payment.version),
        }

    @staticmethod
    def _dict_to_payment(data: Mapping[str, object]) -> PaymentRecord:
        return PaymentRecord(
            payment_id=str(data.get("payment_id") or ""),
            tenant_id=str(data.get("tenant_id") or ""),
            provider=str(data.get("provider") or ""),
            amount=float(data.get("amount") or 0),
            currency=str(data.get("currency") or "RUB"),
            status=str(data.get("status") or PAY_CREATED),
            payment_method_type=str(data.get("payment_method_type") or ""),
            external_transaction_id=str(data.get("external_transaction_id") or ""),
            order_refs=tuple(data.get("order_refs") or ()),
            invoice_refs=tuple(data.get("invoice_refs") or ()),
            payer_ref=str(data.get("payer_ref") or ""),
            payer_inn=str(data.get("payer_inn") or ""),
            payer_name=str(data.get("payer_name") or ""),
            authorized_amount=float(data.get("authorized_amount") or 0),
            captured_amount=float(data.get("captured_amount") or 0),
            refunded_amount=float(data.get("refunded_amount") or 0),
            occurred_at=_parse_dt(data.get("occurred_at")),
            received_at=_parse_dt(data.get("received_at")) or _utc(),
            source=str(data.get("source") or "gateway"),
            provenance=dict(data.get("provenance") or {}),
            metadata=dict(data.get("metadata") or {}),
            version=int(data.get("version") or 1),
        )

    @staticmethod
    def _bank_to_dict(tx: BankTransaction) -> dict:
        return {
            "transaction_id": tx.transaction_id,
            "tenant_id": tx.tenant_id,
            "account_ref": tx.account_ref,
            "amount": float(tx.amount),
            "currency": tx.currency,
            "direction": tx.direction,
            "external_bank_id": tx.external_bank_id,
            "booked_at": _iso(tx.booked_at),
            "value_date": _iso(tx.value_date),
            "payer_ref": tx.payer_ref,
            "payee_ref": tx.payee_ref,
            "payer_inn": tx.payer_inn,
            "payer_name": tx.payer_name,
            "purpose": tx.purpose,
            "document_ref": tx.document_ref,
            "invoice_ref": tx.invoice_ref,
            "order_ref": tx.order_ref,
            "source_statement_ref": tx.source_statement_ref,
            "provenance": dict(tx.provenance),
            "metadata": dict(tx.metadata),
        }

    @staticmethod
    def _dict_to_bank(data: Mapping[str, object]) -> BankTransaction:
        return BankTransaction(
            transaction_id=str(data.get("transaction_id") or ""),
            tenant_id=str(data.get("tenant_id") or ""),
            account_ref=str(data.get("account_ref") or ""),
            amount=float(data.get("amount") or 0),
            currency=str(data.get("currency") or "RUB"),
            direction=str(data.get("direction") or "incoming"),
            external_bank_id=str(data.get("external_bank_id") or ""),
            booked_at=_parse_dt(data.get("booked_at")),
            value_date=_parse_dt(data.get("value_date")),
            payer_ref=str(data.get("payer_ref") or ""),
            payee_ref=str(data.get("payee_ref") or ""),
            payer_inn=str(data.get("payer_inn") or ""),
            payer_name=str(data.get("payer_name") or ""),
            purpose=str(data.get("purpose") or ""),
            document_ref=str(data.get("document_ref") or ""),
            invoice_ref=str(data.get("invoice_ref") or ""),
            order_ref=str(data.get("order_ref") or ""),
            source_statement_ref=str(data.get("source_statement_ref") or ""),
            provenance=dict(data.get("provenance") or {}),
            metadata=dict(data.get("metadata") or {}),
        )

    @staticmethod
    def _target_to_dict(target: OrderPaymentTarget) -> dict:
        return {
            "order_id": target.order_id,
            "tenant_id": target.tenant_id,
            "amount": float(target.amount),
            "currency": target.currency,
            "invoice_number": target.invoice_number,
            "buyer_inn": target.buyer_inn,
            "buyer_name": target.buyer_name,
            "buyer_ref": target.buyer_ref,
            "payment_reference": target.payment_reference,
            "fulfillment_state": target.fulfillment_state,
            "shipment_started": bool(target.shipment_started),
            "marking_incomplete": bool(target.marking_incomplete),
            "fiscal_receipt_ref": target.fiscal_receipt_ref,
            "fiscal_amount": target.fiscal_amount,
            "cancelled": bool(target.cancelled),
        }

    @staticmethod
    def _dict_to_target(data: Mapping[str, object]) -> OrderPaymentTarget:
        fiscal_amount = data.get("fiscal_amount")
        return OrderPaymentTarget(
            order_id=str(data.get("order_id") or ""),
            tenant_id=str(data.get("tenant_id") or ""),
            amount=float(data.get("amount") or 0),
            currency=str(data.get("currency") or "RUB"),
            invoice_number=str(data.get("invoice_number") or ""),
            buyer_inn=str(data.get("buyer_inn") or ""),
            buyer_name=str(data.get("buyer_name") or ""),
            buyer_ref=str(data.get("buyer_ref") or ""),
            payment_reference=str(data.get("payment_reference") or ""),
            fulfillment_state=str(data.get("fulfillment_state") or ""),
            shipment_started=bool(data.get("shipment_started")),
            marking_incomplete=bool(data.get("marking_incomplete")),
            fiscal_receipt_ref=str(data.get("fiscal_receipt_ref") or ""),
            fiscal_amount=float(fiscal_amount) if fiscal_amount is not None else None,
            cancelled=bool(data.get("cancelled")),
        )

    @staticmethod
    def _match_to_dict(match: PaymentMatchResult) -> dict:
        return {
            "match_id": match.match_id,
            "tenant_id": match.tenant_id,
            "payment_id": match.payment_id,
            "bank_transaction_id": match.bank_transaction_id,
            "candidate_order_refs": list(match.candidate_order_refs),
            "candidate_invoice_refs": list(match.candidate_invoice_refs),
            "selected_order_id": match.selected_order_id,
            "selected_invoice_id": match.selected_invoice_id,
            "evidence": dict(match.evidence),
            "conflicts": list(match.conflicts),
            "confidence": float(match.confidence),
            "review_required": bool(match.review_required),
            "status": match.status,
            "created_at": _iso(match.created_at) or _utc().isoformat(),
        }

    @staticmethod
    def _refund_to_dict(refund: RefundRecord) -> dict:
        return {
            "refund_id": refund.refund_id,
            "payment_id": refund.payment_id,
            "tenant_id": refund.tenant_id,
            "amount": float(refund.amount),
            "currency": refund.currency,
            "status": refund.status,
            "order_id": refund.order_id,
            "reason": refund.reason,
            "external_ref": refund.external_ref,
            "prepared_by": refund.prepared_by,
            "approved_by": refund.approved_by,
            "executed_at": _iso(refund.executed_at),
            "provenance": dict(refund.provenance),
            "metadata": dict(refund.metadata),
            "idempotency_key": refund.idempotency_key,
        }

    @staticmethod
    def _dict_to_refund(data: Mapping[str, object]) -> RefundRecord:
        return RefundRecord(
            refund_id=str(data.get("refund_id") or ""),
            payment_id=str(data.get("payment_id") or ""),
            tenant_id=str(data.get("tenant_id") or ""),
            amount=float(data.get("amount") or 0),
            currency=str(data.get("currency") or "RUB"),
            status=str(data.get("status") or REF_REQUESTED),
            order_id=str(data.get("order_id") or ""),
            reason=str(data.get("reason") or ""),
            external_ref=str(data.get("external_ref") or ""),
            prepared_by=str(data.get("prepared_by") or ""),
            approved_by=str(data.get("approved_by") or ""),
            executed_at=_parse_dt(data.get("executed_at")),
            provenance=dict(data.get("provenance") or {}),
            metadata=dict(data.get("metadata") or {}),
            idempotency_key=str(data.get("idempotency_key") or ""),
        )

    def _get_payment(self, tenant_id: str, payment_id: str) -> PaymentRecord:
        data = self.store.get_payment(tenant_id, payment_id)
        if data is None:
            raise TenantAccessDeniedError("tenant_access_denied")
        return self._dict_to_payment(data)

    def _save_payment(self, payment: PaymentRecord) -> PaymentRecord:
        payload = self._payment_to_dict(payment)
        assert_no_card_data(payload)
        self.store.save_payment(payment.tenant_id, payment.payment_id, payload, payment.status)
        return payment

    def _transition_payment(self, payment: PaymentRecord, target: str) -> PaymentRecord:
        assert_transition("payment", payment.status, target)
        if payment.status == target:
            return payment
        updated = replace(payment, status=target, version=int(payment.version) + 1)
        return self._save_payment(updated)

    def _list_targets(self, tenant_id: str) -> list[OrderPaymentTarget]:
        return [self._dict_to_target(t) for t in self.store.list_targets(tenant_id)]

    def _active_allocations(
        self, tenant_id: str, *, payment_id: str = "", order_id: str = ""
    ) -> list[dict]:
        rows = self.store.list_allocations(tenant_id, payment_id=payment_id, order_id=order_id)
        return [r for r in rows if str(r.get("status") or "") != ALLOC_SUPERSEDED]

    # ---- order targets ----

    def register_order_target(self, tenant_id: str, target: OrderPaymentTarget) -> OrderPaymentTarget:
        tenant = normalize_tenant_id(tenant_id)
        if normalize_tenant_id(target.tenant_id) != tenant:
            raise TenantAccessDeniedError("tenant_access_denied")
        payload = self._target_to_dict(target)
        assert_no_card_data(payload)
        self.store.save_target(tenant, target.order_id, payload)
        self.store.audit(
            tenant,
            "order_target_registered",
            refs={"order_id": target.order_id},
            details={"amount": target.amount, "currency": target.currency},
        )
        return target

    # ---- payments ----

    def detect_duplicate_payment(
        self, tenant_id: str, *, external_transaction_id: str, payment_id: str = ""
    ) -> dict | None:
        ext = str(external_transaction_id or "").strip()
        if not ext:
            return None
        existing = self.store.get_payment_by_external(tenant_id, ext)
        if existing is None:
            return None
        if payment_id and str(existing.get("payment_id") or "") == payment_id:
            return None
        self._inc("duplicate_payments_detected")
        self.store.audit(
            tenant_id,
            "duplicate_payment_detected",
            refs={
                "external_transaction_id": ext,
                "existing_payment_id": str(existing.get("payment_id") or ""),
                "incoming_payment_id": payment_id,
            },
            details={"status": UNLOCK_REVIEW},
        )
        return existing

    def record_payment(self, payment: PaymentRecord) -> PaymentRecord:
        assert_no_card_data(payment.metadata)
        assert_no_card_data(payment.provenance)
        tenant = normalize_tenant_id(payment.tenant_id)
        dup = self.detect_duplicate_payment(
            tenant,
            external_transaction_id=payment.external_transaction_id,
            payment_id=payment.payment_id,
        )
        if dup is not None:
            existing = self._dict_to_payment(dup)
            self.store.audit(
                tenant,
                "payment_record_idempotent",
                refs={"payment_id": existing.payment_id},
                details={"external_transaction_id": payment.external_transaction_id},
            )
            return existing
        saved = self._save_payment(payment)
        self._inc("payments_recorded")
        self.store.audit(
            tenant,
            "payment_recorded",
            refs={"payment_id": saved.payment_id},
            details={"status": saved.status, "amount": saved.amount},
        )
        return saved

    def process_webhook_event(
        self,
        tenant_id: str,
        provider: str,
        event_id: str,
        provider_event_type: str,
        payload: Mapping[str, object] | None,
        *,
        signature_verified: bool = True,
    ) -> dict:
        """Normalize provider event. Signature must already be verified upstream."""
        if not signature_verified:
            raise PolicyDeniedError("policy_denied")
        tenant = normalize_tenant_id(tenant_id)
        raw = dict(payload or {})
        assert_no_card_data(raw)
        canonical = normalize_event_type(provider_event_type)
        safe = extract_safe_payment_fields(raw)
        event_payload = {
            "provider": provider,
            "provider_event_type": provider_event_type,
            "canonical_event_type": canonical,
            "safe_fields": safe,
            "event_id": event_id,
        }
        inserted = self.store.save_event(tenant, event_id, canonical, event_payload)
        if not inserted:
            self._inc("webhooks_duplicate")
            self.store.audit(
                tenant,
                "webhook_duplicate",
                refs={"event_id": event_id},
                details={"provider": provider, "event_type": canonical},
            )
            existing = None
            if safe.get("external_transaction_id"):
                existing = self.store.get_payment_by_external(
                    tenant, str(safe["external_transaction_id"])
                )
            return {
                "status": "duplicate",
                "event_id": event_id,
                "payment_id": str((existing or {}).get("payment_id") or ""),
                "canonical_event_type": canonical,
            }

        target_status = event_to_payment_status(canonical)
        ext_id = str(safe.get("external_transaction_id") or "")
        payment_id = str(safe.get("payment_id") or "")
        existing_data = None
        if ext_id:
            existing_data = self.store.get_payment_by_external(tenant, ext_id)
        if existing_data is None and payment_id:
            existing_data = self.store.get_payment(tenant, payment_id)

        order_ref = str(safe.get("order_ref") or "")
        invoice_ref = str(safe.get("invoice_ref") or "")
        amount = float(safe.get("amount") or 0)
        currency = str(safe.get("currency") or "RUB")

        if existing_data is None:
            pid = payment_id or _new_id("pay")
            status = target_status or PAY_CREATED
            payment = PaymentRecord(
                payment_id=pid,
                tenant_id=tenant,
                provider=provider,
                amount=amount,
                currency=currency,
                status=PAY_CREATED,
                payment_method_type=str(safe.get("payment_method_type") or ""),
                external_transaction_id=ext_id,
                order_refs=(order_ref,) if order_ref else (),
                invoice_refs=(invoice_ref,) if invoice_ref else (),
                payer_ref=str(safe.get("payer_ref") or ""),
                payer_inn=str(safe.get("payer_inn") or ""),
                payer_name=str(safe.get("payer_name") or ""),
                captured_amount=amount if status in {PAY_PAID} else 0.0,
                authorized_amount=amount if status == "AUTHORIZED" else 0.0,
                source="webhook",
                provenance={"event_id": event_id, "provider": provider},
                metadata={"masked_method": safe.get("masked_method") or ""},
            )
            if status != PAY_CREATED:
                assert_transition("payment", PAY_CREATED, status)
                payment = replace(payment, status=status)
            self._save_payment(payment)
            self._inc("payments_recorded")
        else:
            payment = self._dict_to_payment(existing_data)
            if target_status and target_status != payment.status:
                payment = self._transition_payment(payment, target_status)
            # merge safe refs without commerce mutation
            order_refs = tuple(
                dict.fromkeys(list(payment.order_refs) + ([order_ref] if order_ref else []))
            )
            invoice_refs = tuple(
                dict.fromkeys(list(payment.invoice_refs) + ([invoice_ref] if invoice_ref else []))
            )
            updates: dict[str, Any] = {
                "order_refs": order_refs,
                "invoice_refs": invoice_refs,
                "version": int(payment.version) + 1,
            }
            if amount > 0 and payment.amount <= 0:
                updates["amount"] = amount
            if safe.get("payer_inn") and not payment.payer_inn:
                updates["payer_inn"] = str(safe["payer_inn"])
            if safe.get("payer_name") and not payment.payer_name:
                updates["payer_name"] = str(safe["payer_name"])
            if target_status == PAY_PAID:
                updates["captured_amount"] = amount or payment.amount
            payment = replace(payment, **updates)
            self._save_payment(payment)

        self._inc("webhooks_processed")
        self.store.audit(
            tenant,
            "webhook_processed",
            refs={"event_id": event_id, "payment_id": payment.payment_id},
            details={
                "provider": provider,
                "canonical_event_type": canonical,
                "status": payment.status,
            },
        )
        # Never mutate commerce inline — enqueue async processing only.
        wf = self.enqueue_workflow(
            "payments.process_event",
            tenant_id=tenant,
            execution_key=f"payments-event:{tenant}:{event_id}",
            metadata={
                "tenant_id": tenant,
                "event_id": event_id,
                "payment_id": payment.payment_id,
                "provider": provider,
                "canonical_event_type": canonical,
            },
        )
        return {
            "status": "processed",
            "event_id": event_id,
            "payment_id": payment.payment_id,
            "payment_status": payment.status,
            "canonical_event_type": canonical,
            "workflow": wf,
        }

    # ---- bank ingest ----

    def ingest_bank_transactions(
        self,
        tenant_id: str,
        transactions: list[BankTransaction] | list[dict],
        statement_ref: str = "",
    ) -> dict:
        tenant = normalize_tenant_id(tenant_id)
        imported = 0
        deduped = 0
        ids: list[str] = []
        for item in transactions:
            if isinstance(item, BankTransaction):
                tx = item
                if statement_ref and not tx.source_statement_ref:
                    tx = replace(tx, source_statement_ref=statement_ref)
            else:
                raw = dict(item)
                assert_no_card_data(raw)
                if statement_ref and not raw.get("source_statement_ref"):
                    raw["source_statement_ref"] = statement_ref
                if not raw.get("transaction_id"):
                    raw["transaction_id"] = _new_id("btx")
                raw["tenant_id"] = tenant
                tx = self._dict_to_bank(raw)
            payload = self._bank_to_dict(tx)
            ok = self.store.save_bank_tx(tenant, tx.transaction_id, payload)
            if ok:
                imported += 1
                ids.append(tx.transaction_id)
                self._inc("bank_tx_ingested")
            else:
                deduped += 1
                self._inc("bank_tx_deduped")
        if statement_ref:
            self.store.save_statement(
                tenant,
                statement_ref,
                {
                    "statement_ref": statement_ref,
                    "account_ref": "",
                    "imported": imported,
                    "deduped": deduped,
                    "transaction_ids": ids,
                },
            )
        self.store.audit(
            tenant,
            "bank_transactions_ingested",
            refs={"statement_ref": statement_ref},
            details={"imported": imported, "deduped": deduped},
        )
        return {"imported": imported, "deduped": deduped, "transaction_ids": ids}

    def ingest_statement_rows(
        self,
        tenant_id: str,
        rows: list[dict],
        *,
        account_ref: str,
        statement_ref: str,
        period_start: str = "",
        period_end: str = "",
    ) -> dict:
        tenant = normalize_tenant_id(tenant_id)
        txs: list[BankTransaction] = []
        for idx, row in enumerate(rows):
            assert_no_card_data(row)
            amount_raw = (
                row.get("amount")
                or row.get("sum")
                or row.get("сумма")
                or row.get("Amount")
                or 0
            )
            try:
                amount = float(str(amount_raw).replace(" ", "").replace(",", "."))
            except (TypeError, ValueError):
                amount = 0.0
            currency = str(
                row.get("currency") or row.get("валюта") or row.get("Currency") or "RUB"
            )
            purpose = str(
                row.get("purpose")
                or row.get("payment_purpose")
                or row.get("назначение")
                or row.get("description")
                or ""
            )
            inn = str(row.get("inn") or row.get("payer_inn") or row.get("инн") or "")
            invoice = str(
                row.get("invoice")
                or row.get("invoice_ref")
                or row.get("invoice_number")
                or row.get("счёт")
                or ""
            )
            order_ref = str(
                row.get("order") or row.get("order_ref") or row.get("order_id") or row.get("заказ") or ""
            )
            ext = str(
                row.get("external_bank_id")
                or row.get("bank_id")
                or row.get("document_number")
                or row.get("doc_no")
                or f"{statement_ref}:{idx}"
            )
            direction = str(row.get("direction") or ("incoming" if amount >= 0 else "outgoing"))
            txs.append(
                BankTransaction(
                    transaction_id=_new_id("btx"),
                    tenant_id=tenant,
                    account_ref=account_ref,
                    amount=abs(amount),
                    currency=currency,
                    direction=direction,
                    external_bank_id=ext,
                    booked_at=_parse_dt(row.get("booked_at") or row.get("date") or row.get("дата")),
                    value_date=_parse_dt(row.get("value_date") or row.get("value_date_at")),
                    payer_ref=str(row.get("payer_ref") or row.get("payer") or ""),
                    payee_ref=str(row.get("payee_ref") or row.get("payee") or ""),
                    payer_inn=inn,
                    payer_name=str(row.get("payer_name") or row.get("name") or row.get("контрагент") or ""),
                    purpose=purpose,
                    document_ref=str(row.get("document_ref") or row.get("document_number") or ""),
                    invoice_ref=invoice,
                    order_ref=order_ref,
                    source_statement_ref=statement_ref,
                    provenance={"row_index": idx, "statement_ref": statement_ref},
                    metadata={},
                )
            )
        self.store.save_statement(
            tenant,
            statement_ref,
            {
                "statement_ref": statement_ref,
                "account_ref": account_ref,
                "period_start": period_start,
                "period_end": period_end,
                "row_count": len(rows),
            },
        )
        result = self.ingest_bank_transactions(tenant, txs, statement_ref=statement_ref)
        result["statement_ref"] = statement_ref
        result["account_ref"] = account_ref
        return result

    # ---- matching ----

    def match_payment(self, tenant_id: str, payment_id: str) -> PaymentMatchResult:
        tenant = normalize_tenant_id(tenant_id)
        payment = self._get_payment(tenant, payment_id)
        targets = self._list_targets(tenant)
        result = self.matcher.match_payment(payment, targets)
        self.store.save_match(tenant, result.match_id, self._match_to_dict(result))
        self._inc("matches_created")
        if result.review_required:
            self._inc("matches_review")
            if payment.status not in {PAY_RECONCILIATION_REQUIRED}:
                try:
                    self._transition_payment(payment, PAY_RECONCILIATION_REQUIRED)
                except Exception:
                    pass
            self.store.audit(
                tenant,
                "payment_match_review_required",
                refs={"payment_id": payment_id, "match_id": result.match_id},
                details={"status": UNLOCK_REVIEW, "confidence": result.confidence},
            )
            self._request_hitl(
                tenant_id=tenant,
                action_id=f"match-review-{result.match_id}",
                workflow_id=f"payments-match:{payment_id}",
                task_id=result.match_id,
                operation="match_review",
                resource=f"tenant:{tenant}:payment:{payment_id}",
                reason_code="payment_match_review_required",
                metadata={"match_id": result.match_id, "status": UNLOCK_REVIEW},
            )
        else:
            self.store.audit(
                tenant,
                "payment_matched",
                refs={
                    "payment_id": payment_id,
                    "match_id": result.match_id,
                    "order_id": result.selected_order_id,
                },
                details={"confidence": result.confidence},
            )
        return result

    def match_bank_tx(self, tenant_id: str, transaction_id: str) -> PaymentMatchResult:
        tenant = normalize_tenant_id(tenant_id)
        rows = self.store.list_bank_tx(tenant)
        found = next((r for r in rows if str(r.get("transaction_id")) == transaction_id), None)
        if found is None:
            raise TenantAccessDeniedError("tenant_access_denied")
        tx = self._dict_to_bank(found)
        targets = self._list_targets(tenant)
        result = self.matcher.match_bank_transaction(tx, targets)
        self.store.save_match(tenant, result.match_id, self._match_to_dict(result))
        self._inc("matches_created")
        if result.review_required:
            self._inc("matches_review")
            self.store.audit(
                tenant,
                "bank_match_review_required",
                refs={"transaction_id": transaction_id, "match_id": result.match_id},
                details={"status": UNLOCK_REVIEW, "confidence": result.confidence},
            )
            self._request_hitl(
                tenant_id=tenant,
                action_id=f"bank-match-review-{result.match_id}",
                workflow_id=f"payments-bank-match:{transaction_id}",
                task_id=result.match_id,
                operation="bank_match_review",
                resource=f"tenant:{tenant}:bank_tx:{transaction_id}",
                reason_code="payment_match_review_required",
                metadata={"match_id": result.match_id, "status": UNLOCK_REVIEW},
            )
        else:
            self.store.audit(
                tenant,
                "bank_matched",
                refs={
                    "transaction_id": transaction_id,
                    "match_id": result.match_id,
                    "order_id": result.selected_order_id,
                },
                details={"confidence": result.confidence},
            )
        return result

    # ---- allocation ----

    def allocated_total_for_order(self, tenant_id: str, order_id: str) -> float:
        rows = self._active_allocations(tenant_id, order_id=order_id)
        return sum(float(r.get("allocated_amount") or 0) for r in rows)

    def remaining_for_order(self, tenant_id: str, order_id: str) -> float:
        target_data = self.store.get_target(tenant_id, order_id)
        if target_data is None:
            return 0.0
        target = self._dict_to_target(target_data)
        remaining = float(target.amount) - self.allocated_total_for_order(tenant_id, order_id)
        return max(0.0, remaining)

    def allocate(
        self,
        tenant_id: str,
        payment_id: str,
        order_id: str,
        amount: float,
        *,
        invoice_id: str = "",
        method: str = "manual",
        capabilities: tuple[str, ...] = (),
        confidence: float = 1.0,
        idempotency_key: str = "",
    ) -> PaymentAllocation:
        self._require_cap(capabilities, CAP_PAYMENTS_ALLOCATE)
        tenant = normalize_tenant_id(tenant_id)
        payment = self._get_payment(tenant, payment_id)
        amt = float(amount)
        if amt <= 0:
            raise PolicyDeniedError("policy_denied")

        key = idempotency_key or f"alloc:{tenant}:{payment_id}:{order_id}:{amt:.2f}:{invoice_id}"
        op_id = _new_id("pop")
        existing = self.store.begin_op(
            tenant,
            op_id,
            "allocate",
            key,
            {"operation_id": op_id, "payment_id": payment_id, "order_id": order_id, "amount": amt},
        )
        if existing is not None:
            alloc_id = str(existing.get("allocation_id") or "")
            if alloc_id:
                rows = self.store.list_allocations(tenant, payment_id=payment_id, order_id=order_id)
                for row in rows:
                    if str(row.get("allocation_id")) == alloc_id:
                        return PaymentAllocation(
                            allocation_id=alloc_id,
                            payment_id=payment_id,
                            tenant_id=tenant,
                            allocated_amount=float(row.get("allocated_amount") or amt),
                            currency=str(row.get("currency") or payment.currency),
                            order_id=order_id,
                            invoice_id=str(row.get("invoice_id") or invoice_id),
                            allocation_method=str(row.get("allocation_method") or method),
                            evidence=dict(row.get("evidence") or {}),
                            confidence=float(row.get("confidence") or confidence),
                            status=str(row.get("status") or ALLOC_CONFIRMED),
                            created_at=_parse_dt(row.get("created_at")) or _utc(),
                            superseded_by=str(row.get("superseded_by") or ""),
                        )

        target_data = self.store.get_target(tenant, order_id)
        if target_data is not None:
            target = self._dict_to_target(target_data)
            if self.policy_engine.active().currency_strict and target.currency != payment.currency:
                raise CurrencyMismatchError("currency_mismatch")

        # Never silent overwrite: supersede prior active allocation for same payment+order.
        prior = self._active_allocations(tenant, payment_id=payment_id, order_id=order_id)
        alloc_id = _new_id("alloc")
        for old in prior:
            old_id = str(old.get("allocation_id") or "")
            if old_id:
                self.store.supersede_allocation(tenant, old_id, superseded_by=alloc_id)

        allocation = PaymentAllocation(
            allocation_id=alloc_id,
            payment_id=payment_id,
            tenant_id=tenant,
            allocated_amount=amt,
            currency=payment.currency,
            order_id=order_id,
            invoice_id=invoice_id,
            allocation_method=method,
            evidence={"method": method, "superseded_count": len(prior)},
            confidence=float(confidence),
            status=ALLOC_CONFIRMED,
        )
        payload = {
            "allocation_id": allocation.allocation_id,
            "payment_id": payment_id,
            "tenant_id": tenant,
            "allocated_amount": amt,
            "currency": payment.currency,
            "order_id": order_id,
            "invoice_id": invoice_id,
            "allocation_method": method,
            "evidence": dict(allocation.evidence),
            "confidence": float(confidence),
            "status": ALLOC_CONFIRMED,
            "created_at": _iso(allocation.created_at),
            "superseded_by": "",
        }
        self.store.save_allocation(tenant, alloc_id, payload)
        self.store.complete_op(
            tenant,
            op_id if existing is None else str(existing.get("operation_id") or op_id),
            "completed",
            {"allocation_id": alloc_id, "operation_id": op_id, "status": "completed"},
        )
        self._inc("allocations_created")

        # Update payment status from allocation totals vs payment / order.
        pay_alloc = sum(
            float(r.get("allocated_amount") or 0)
            for r in self._active_allocations(tenant, payment_id=payment_id)
        )
        pol = self.policy_engine.active()
        new_status = payment.status
        if target_data is not None:
            order_total = self.allocated_total_for_order(tenant, order_id)
            required = float(target_data.get("amount") or 0)
            if self.policy_engine.within_tolerance(required, order_total, pol):
                new_status = PAY_PAID
            elif order_total > required + pol.amount_tolerance:
                new_status = PAY_OVERPAID
            elif order_total > 0:
                new_status = PAY_PARTIALLY_PAID if pay_alloc + pol.amount_tolerance < payment.amount else PAY_UNDERPAID
        else:
            if self.policy_engine.within_tolerance(payment.amount, pay_alloc, pol):
                new_status = PAY_PAID
            elif pay_alloc > payment.amount + pol.amount_tolerance:
                new_status = PAY_OVERPAID
            elif pay_alloc > 0:
                new_status = PAY_PARTIALLY_PAID

        if new_status != payment.status:
            try:
                self._transition_payment(payment, new_status)
            except Exception:
                # Allocation history is source of truth; status update best-effort when illegal.
                self.store.audit(
                    tenant,
                    "allocation_status_deferred",
                    refs={"payment_id": payment_id, "allocation_id": alloc_id},
                    details={"from": payment.status, "to": new_status},
                )

        self.store.audit(
            tenant,
            "payment_allocated",
            refs={"payment_id": payment_id, "order_id": order_id, "allocation_id": alloc_id},
            details={"amount": amt, "method": method},
        )
        return allocation

    # ---- fulfillment unlock ----

    def evaluate_fulfillment_unlock(
        self, tenant_id: str, order_id: str
    ) -> FulfillmentUnlockResult:
        tenant = normalize_tenant_id(tenant_id)
        pol = self.policy_engine.active()
        target_data = self.store.get_target(tenant, order_id)
        if target_data is None:
            return FulfillmentUnlockResult(
                code=UNLOCK_NOT_CONFIRMED,
                tenant_id=tenant,
                order_id=order_id,
                required_amount=0.0,
                allocated_amount=0.0,
                remaining_amount=0.0,
                currency="RUB",
                review_required=False,
                evidence={"reason": "no_target"},
                policy_version=pol.version,
            )
        target = self._dict_to_target(target_data)
        allocated = self.allocated_total_for_order(tenant, order_id)
        remaining = max(0.0, target.amount - allocated)
        alloc_rows = self._active_allocations(tenant, order_id=order_id)
        payment_ids = tuple(
            dict.fromkeys(str(r.get("payment_id") or "") for r in alloc_rows if r.get("payment_id"))
        )

        code = UNLOCK_NOT_CONFIRMED
        review = False
        evidence: dict[str, object] = {
            "allocated": allocated,
            "required": target.amount,
            "policy_id": pol.policy_id,
        }

        if target.cancelled:
            code = UNLOCK_BLOCKED
            evidence["reason"] = "order_cancelled"
        else:
            # payer / INN mismatch on linked payments
            mismatch = False
            for pid in payment_ids:
                pdata = self.store.get_payment(tenant, pid)
                if not pdata:
                    continue
                pay = self._dict_to_payment(pdata)
                if pay.payer_inn and target.buyer_inn and pay.payer_inn != target.buyer_inn:
                    mismatch = True
                    evidence["inn_mismatch"] = True
                    break
            if mismatch:
                if pol.payer_mismatch_behavior == "block":
                    code = UNLOCK_BLOCKED
                else:
                    code = UNLOCK_REVIEW
                    review = True
            elif allocated <= 0:
                code = UNLOCK_NOT_CONFIRMED
            elif allocated + pol.amount_tolerance < target.amount:
                if pol.allow_partial_fulfillment:
                    code = UNLOCK_PARTIAL
                else:
                    code = UNLOCK_PARTIAL
                    evidence["partial_not_allowed"] = not pol.allow_partial_fulfillment
                if not pol.allow_partial_fulfillment and pol.fulfillment_requires_confirmed:
                    # still partial — do not confirm
                    pass
            elif allocated > target.amount + pol.amount_tolerance:
                if pol.overpayment_behavior == "review":
                    code = UNLOCK_REVIEW
                    review = True
                else:
                    code = UNLOCK_CONFIRMED
                    evidence["overpayment"] = True
            else:
                # within tolerance
                if pol.fulfillment_requires_confirmed:
                    code = UNLOCK_CONFIRMED
                else:
                    code = UNLOCK_CONFIRMED

        if code == UNLOCK_CONFIRMED:
            self._inc("unlock_confirmed")
        elif code == UNLOCK_BLOCKED:
            self._inc("unlock_blocked")

        return FulfillmentUnlockResult(
            code=code,
            tenant_id=tenant,
            order_id=order_id,
            required_amount=float(target.amount),
            allocated_amount=float(allocated),
            remaining_amount=float(remaining),
            currency=target.currency,
            payment_ids=payment_ids,
            review_required=review or code == UNLOCK_REVIEW,
            evidence=evidence,
            policy_version=pol.version,
        )

    def apply_unlock_to_commerce(self, tenant_id: str, order_id: str) -> FulfillmentUnlockResult:
        """Push confirmed/unconfirmed payment refs to commerce — no circular mutation."""
        unlock = self.evaluate_fulfillment_unlock(tenant_id, order_id)
        tenant = normalize_tenant_id(tenant_id)
        commerce = self.commerce_service
        if commerce is not None and hasattr(commerce, "update_payment_reference"):
            status = "confirmed" if unlock.code == UNLOCK_CONFIRMED else "unconfirmed"
            try:
                commerce.update_payment_reference(
                    tenant_id=tenant,
                    order_id=order_id,
                    payment_status=status,
                    payment_refs=list(unlock.payment_ids),
                    unlock_code=unlock.code,
                )
            except Exception as exc:
                self.store.audit(
                    tenant,
                    "commerce_unlock_failed",
                    refs={"order_id": order_id},
                    details={"error": type(exc).__name__, "code": unlock.code},
                )
        # Resume commerce only via optional hook when confirmed — never circular mutate here.
        if unlock.code == UNLOCK_CONFIRMED and commerce is not None:
            hook = getattr(commerce, "on_payment_confirmed", None) or getattr(
                commerce, "enqueue_resume_after_payment", None
            )
            if callable(hook):
                try:
                    hook(tenant_id=tenant, order_id=order_id, payment_ids=list(unlock.payment_ids))
                except Exception:
                    self.store.audit(
                        tenant,
                        "commerce_resume_hook_failed",
                        refs={"order_id": order_id},
                        details={"code": unlock.code},
                    )
        self.store.audit(
            tenant,
            "fulfillment_unlock_applied",
            refs={"order_id": order_id},
            details={"code": unlock.code, "allocated": unlock.allocated_amount},
        )
        return unlock

    # ---- refunds ----

    def prepare_refund(
        self,
        tenant_id: str,
        payment_id: str,
        amount: float,
        *,
        order_id: str = "",
        reason: str = "",
        prepared_by: str = "",
        capabilities: tuple[str, ...] = (),
        idempotency_key: str = "",
        currency: str = "",
    ) -> RefundRecord:
        self._require_cap(capabilities, CAP_PAYMENTS_PREPARE_REFUND)
        tenant = normalize_tenant_id(tenant_id)
        payment = self._get_payment(tenant, payment_id)
        amt = float(amount)
        cur = str(currency or payment.currency).upper()
        key = idempotency_key or f"refund-prep:{tenant}:{payment_id}:{amt:.2f}:{order_id}"

        existing = self.store.get_refund_by_idempotency(tenant, key)
        if existing is not None:
            return self._dict_to_refund(existing)

        refund = RefundRecord(
            refund_id=_new_id("ref"),
            payment_id=payment_id,
            tenant_id=tenant,
            amount=amt,
            currency=cur,
            status=REF_REQUESTED,
            order_id=order_id,
            reason=reason,
            prepared_by=prepared_by,
            idempotency_key=key,
            provenance={"payment_external_id": payment.external_transaction_id},
        )
        self.store.save_refund(tenant, refund.refund_id, self._refund_to_dict(refund), refund.status)

        conf = self.payment_gateway.prepare_refund(
            tenant_id=tenant,
            payment_external_id=payment.external_transaction_id or payment.payment_id,
            amount=amt,
            currency=cur,
            idempotency_key=key,
        )
        assert_transition("refund", REF_REQUESTED, REF_PREPARED)
        refund = replace(
            refund,
            status=REF_PREPARED,
            external_ref=str(getattr(conf, "external_id", "") or ""),
            metadata={**dict(refund.metadata), "prepare_status": getattr(conf, "status", "")},
        )
        self.store.save_refund(tenant, refund.refund_id, self._refund_to_dict(refund), refund.status)

        pol = self.policy_engine.active()
        if self.policy_engine.refund_requires_hitl(amt, policy=pol):
            assert_transition("refund", REF_PREPARED, REF_AWAITING_APPROVAL)
            refund = replace(refund, status=REF_AWAITING_APPROVAL)
            self.store.save_refund(tenant, refund.refund_id, self._refund_to_dict(refund), refund.status)
            self._request_hitl(
                tenant_id=tenant,
                action_id=f"refund-prep-{refund.refund_id}",
                workflow_id=f"payments-refund:{refund.refund_id}",
                task_id=refund.refund_id,
                operation="prepare_refund",
                resource=f"tenant:{tenant}:refund:{refund.refund_id}",
                reason_code="refund_approval_required",
                capabilities_checked=(CAP_PAYMENTS_PREPARE_REFUND,),
                metadata={
                    "refund_id": refund.refund_id,
                    "amount": amt,
                    "idempotency_key": key,
                },
                requested_by="payments.prepare_refund",
            )

        try:
            self._transition_payment(payment, PAY_REFUND_PENDING)
        except Exception:
            pass

        self._inc("refunds_prepared")
        self.store.audit(
            tenant,
            "refund_prepared",
            refs={"refund_id": refund.refund_id, "payment_id": payment_id},
            details={"amount": amt, "status": refund.status},
        )
        return refund

    def execute_refund(
        self,
        tenant_id: str,
        refund_id: str,
        *,
        capabilities: tuple[str, ...] = (),
        approval_id: str = "",
        approved_by: str = "",
        require_approval: bool = True,
        idempotency_key: str = "",
    ) -> RefundRecord:
        self._require_cap(capabilities, CAP_PAYMENTS_EXECUTE_REFUND)
        tenant = normalize_tenant_id(tenant_id)

        key = idempotency_key
        if key:
            by_key = self.store.get_refund_by_idempotency(tenant, key)
            if by_key is not None and str(by_key.get("refund_id")) != refund_id:
                # Same key bound to another refund — return that record (idempotent).
                return self._dict_to_refund(by_key)

        data = self.store.get_refund(tenant, refund_id)
        if data is None:
            raise TenantAccessDeniedError("tenant_access_denied")
        refund = self._dict_to_refund(data)

        if key and refund.idempotency_key and refund.idempotency_key != key:
            # bind execute key if prepare used a different key
            pass
        exec_key = key or refund.idempotency_key or f"refund-exec:{tenant}:{refund_id}"

        # Idempotent completed refunds
        if refund.status in {REF_CONFIRMED, REF_PARTIAL}:
            return refund

        if require_approval and not approval_id:
            raise PolicyDeniedError("policy_denied")

        if refund.status not in {REF_PREPARED, REF_AWAITING_APPROVAL, REF_UNKNOWN_EXTERNAL, REF_SUBMITTED}:
            if refund.status == REF_REQUESTED:
                raise RefundNotPreparedError("refund_not_prepared")
            raise RefundNotPreparedError("refund_not_prepared")

        if refund.status == REF_AWAITING_APPROVAL:
            assert_transition("refund", REF_AWAITING_APPROVAL, REF_SUBMITTED)
        elif refund.status == REF_PREPARED:
            assert_transition("refund", REF_PREPARED, REF_AWAITING_APPROVAL)
            refund = replace(refund, status=REF_AWAITING_APPROVAL)
            assert_transition("refund", REF_AWAITING_APPROVAL, REF_SUBMITTED)
        elif refund.status == REF_UNKNOWN_EXTERNAL:
            # resume from unknown — allow re-submit path
            assert_transition("refund", REF_UNKNOWN_EXTERNAL, REF_SUBMITTED)

        payment = self._get_payment(tenant, refund.payment_id)
        refund = replace(
            refund,
            status=REF_SUBMITTED,
            approved_by=approved_by or refund.approved_by,
            idempotency_key=refund.idempotency_key or exec_key,
            metadata={**dict(refund.metadata), "approval_id": approval_id},
        )
        self.store.save_refund(tenant, refund.refund_id, self._refund_to_dict(refund), refund.status)

        try:
            conf = self.payment_gateway.execute_refund(
                tenant_id=tenant,
                payment_external_id=payment.external_transaction_id or payment.payment_id,
                amount=refund.amount,
                currency=refund.currency,
                idempotency_key=exec_key,
            )
        except ExternalUnconfirmedError:
            # Timeout / unconfirmed ≠ failed. Mark unknown and query status.
            assert_transition("refund", REF_SUBMITTED, REF_UNKNOWN_EXTERNAL)
            refund = replace(refund, status=REF_UNKNOWN_EXTERNAL)
            self.store.save_refund(tenant, refund.refund_id, self._refund_to_dict(refund), refund.status)
            self._inc("refunds_unknown_external")
            ext_ref = refund.external_ref
            try:
                status_conf = self.payment_gateway.get_refund_status(
                    tenant_id=tenant,
                    refund_external_id=ext_ref or exec_key,
                )
                mapped = str(getattr(status_conf, "status", "") or "")
                if mapped in {REF_CONFIRMED, "CONFIRMED", PAY_REFUNDED}:
                    assert_transition("refund", REF_UNKNOWN_EXTERNAL, REF_CONFIRMED)
                    refund = replace(
                        refund,
                        status=REF_CONFIRMED,
                        external_ref=str(getattr(status_conf, "external_id", "") or ext_ref),
                        executed_at=_utc(),
                    )
                elif mapped in {REF_PARTIAL, "PARTIAL"}:
                    assert_transition("refund", REF_UNKNOWN_EXTERNAL, REF_PARTIAL)
                    refund = replace(refund, status=REF_PARTIAL, executed_at=_utc())
                elif mapped in {REF_FAILED, "FAILED"}:
                    assert_transition("refund", REF_UNKNOWN_EXTERNAL, REF_FAILED)
                    refund = replace(refund, status=REF_FAILED)
                else:
                    # remain UNKNOWN — never treat as failed
                    self.store.save_refund(
                        tenant, refund.refund_id, self._refund_to_dict(refund), refund.status
                    )
                    self.store.audit(
                        tenant,
                        "refund_unknown_external",
                        refs={"refund_id": refund.refund_id},
                        details={"gateway_status": mapped},
                    )
                    return refund
            except ExternalUnconfirmedError:
                self.store.save_refund(
                    tenant, refund.refund_id, self._refund_to_dict(refund), refund.status
                )
                self.store.audit(
                    tenant,
                    "refund_unknown_external",
                    refs={"refund_id": refund.refund_id},
                    details={"gateway_status": "still_unconfirmed"},
                )
                return refund

        else:
            gw_status = str(getattr(conf, "status", "") or REF_CONFIRMED)
            ext = str(getattr(conf, "external_id", "") or refund.external_ref)
            if gw_status in {REF_PARTIAL, "PARTIAL"}:
                assert_transition("refund", REF_SUBMITTED, REF_PARTIAL)
                refund = replace(
                    refund, status=REF_PARTIAL, external_ref=ext, executed_at=_utc()
                )
            elif gw_status in {REF_FAILED, "FAILED"}:
                assert_transition("refund", REF_SUBMITTED, REF_FAILED)
                refund = replace(refund, status=REF_FAILED, external_ref=ext)
            else:
                assert_transition("refund", REF_SUBMITTED, REF_CONFIRMED)
                refund = replace(
                    refund, status=REF_CONFIRMED, external_ref=ext, executed_at=_utc()
                )

        self.store.save_refund(tenant, refund.refund_id, self._refund_to_dict(refund), refund.status)

        if refund.status in {REF_CONFIRMED, REF_PARTIAL}:
            new_refunded = float(payment.refunded_amount) + float(refund.amount)
            pay_updates = {"refunded_amount": new_refunded, "version": int(payment.version) + 1}
            payment = replace(payment, **pay_updates)
            self._save_payment(payment)
            try:
                if new_refunded + 1e-9 >= payment.amount:
                    self._transition_payment(payment, PAY_REFUNDED)
                else:
                    self._transition_payment(payment, PAY_PARTIALLY_REFUNDED)
            except Exception:
                pass
            self._inc("refunds_executed")

        self.store.audit(
            tenant,
            "refund_executed",
            refs={"refund_id": refund.refund_id, "payment_id": refund.payment_id},
            details={"status": refund.status, "approval_id": approval_id},
        )
        return refund

    # ---- reconciliation ----

    def reconcile_tenant(
        self, tenant_id: str, workflow_id: str = "", run_id: str = ""
    ) -> dict:
        tenant = normalize_tenant_id(tenant_id)
        targets = self._list_targets(tenant)
        payments = [self._dict_to_payment(p) for p in self.store.list_payments(tenant)]
        allocations = self.store.list_allocations(tenant)
        bank_txs = self.store.list_bank_tx(tenant)
        refunds = self.store.list_refunds(tenant)
        wf_ref = workflow_id or run_id or ""

        findings = []
        for target in targets:
            findings.extend(
                self.recon_engine.reconcile_order(
                    tenant_id=tenant,
                    target=target,
                    payments=payments,
                    allocations=allocations,
                    bank_txs=bank_txs,
                    refunds=refunds,
                    workflow_ref=wf_ref,
                )
            )
        known_orders = {t.order_id for t in targets}
        findings.extend(
            self.recon_engine.detect_orphan_payments(
                tenant_id=tenant,
                payments=payments,
                allocations=allocations,
                known_orders=known_orders,
                workflow_ref=wf_ref,
            )
        )

        persisted = []
        for finding in findings:
            # auto_corrected always False
            evidence = dict(finding.evidence)
            evidence["auto_corrected"] = False
            payload = {
                "finding_id": finding.finding_id,
                "tenant_id": finding.tenant_id,
                "finding_type": finding.finding_type,
                "severity": finding.severity,
                "status": finding.status,
                "refs": dict(finding.refs),
                "expected": dict(finding.expected),
                "actual": dict(finding.actual),
                "evidence": evidence,
                "created_at": _iso(finding.created_at),
                "resolved_at": _iso(finding.resolved_at) or "",
                "workflow_ref": finding.workflow_ref or wf_ref,
            }
            self.store.save_finding(tenant, finding.finding_id, payload)
            persisted.append(payload)
            if finding.status == "HUMAN_REVIEW":
                self._inc("findings_human_review")
                self._request_hitl(
                    tenant_id=tenant,
                    action_id=f"recon-{finding.finding_id}",
                    workflow_id=wf_ref or f"payments-reconcile:{tenant}",
                    task_id=finding.finding_id,
                    operation="human_review",
                    resource=f"tenant:{tenant}:finding:{finding.finding_id}",
                    reason_code="payments_reconcile_human_review",
                    metadata={
                        "finding_id": finding.finding_id,
                        "finding_type": finding.finding_type,
                        "auto_corrected": False,
                    },
                    requested_by="payments.reconcile",
                )

        self._inc("reconcile_runs")
        self.store.audit(
            tenant,
            "tenant_reconciled",
            refs={"workflow_id": workflow_id, "run_id": run_id},
            details={"findings": len(persisted), "auto_corrected": False},
        )
        return {
            "tenant_id": tenant,
            "findings": persisted,
            "auto_corrected": False,
            "workflow_id": workflow_id,
            "run_id": run_id,
        }

    def enqueue_workflow(
        self,
        workflow_type: str,
        *,
        tenant_id: str,
        execution_key: str,
        metadata: Mapping[str, object] | None = None,
        version: str = "1",
    ) -> dict:
        tenant = normalize_tenant_id(tenant_id)
        wr = self.workflow_runtime
        meta = {"tenant_id": tenant, **dict(metadata or {})}
        if wr is None:
            return {
                "status": "skipped",
                "reason": "workflow_runtime_unavailable",
                "execution_key": execution_key,
                "workflow_type": workflow_type,
            }
        existing = wr.state_manager.find_by_execution_key(execution_key, tenant_id=tenant)
        if existing is not None:
            return {
                "workflow_id": existing.workflow_id,
                "execution_key": execution_key,
                "idempotent": True,
                "status": existing.status,
            }
        created = wr.create_workflow(
            workflow_type,
            version,
            task_id=f"pay-{uuid.uuid4().hex[:8]}",
            execution_key=execution_key,
            metadata=meta,
            tenant_id=tenant,
        )
        enqueued = wr.enqueue_existing(
            created["workflow_id"],
            metadata={"workflow_type": workflow_type, "version": version},
        )
        self._inc("workflows_enqueued")
        return {**enqueued, "execution_key": execution_key, "idempotent": False}

    def enqueue_reconcile(
        self,
        tenant_id: str,
        *,
        reason: str = "event",
        idempotency_key: str | None = None,
    ) -> dict:
        tenant = normalize_tenant_id(tenant_id)
        key = idempotency_key or f"payments-reconcile-event:{tenant}:{reason}"
        wr = self.workflow_runtime
        if wr is None:
            result = self.reconcile_tenant(tenant, workflow_id="", run_id=key)
            return {"status": "inline", "result": result, "execution_key": key}
        return self.enqueue_workflow(
            "payments.reconcile",
            tenant_id=tenant,
            execution_key=key,
            metadata={"tenant_id": tenant, "reason": reason, "trigger": "event"},
        )
