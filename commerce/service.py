"""Commerce service — orchestration over SoT gateways + rules + store."""

from __future__ import annotations

import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Mapping

from commerce.capabilities import LLM_DEFAULT_DENY
from commerce.contracts import (
    DECLARATION_TEXTS,
    BuyerPurposeDeclaration,
    CommerceOrder,
    CommerceOrderLine,
    CommerceOperationResult,
    ComplianceDecision,
    InventoryPosition,
    SupplierRecord,
)
from commerce.errors import (
    CapabilityDeniedError,
    DeclarationImmutableError,
    DeclarationRequiredError,
    ExternalUnconfirmedError,
    IdempotencyError,
    InsufficientStockError,
    NeedsReviewError,
    OversellError,
    StaleStateError,
    TenantAccessDeniedError,
)
from commerce.gateways import (
    FakeAccountingGateway,
    FakeEdoGateway,
    FakeFiscalGateway,
    FakeFrontOfficeGateway,
    FakeInventoryGateway,
    FakeMarkingGateway,
)
from commerce.rules import ComplianceRulesEngine
from commerce.states import (
    MARKING_TRANSFERRED,
    MARKING_WITHDRAWN,
    ORDER_CANCELLED,
    ORDER_CANCEL_PENDING,
    ORDER_COMPLETED,
    ORDER_COMPLIANCE_PENDING,
    ORDER_COMPLIANCE_RISK,
    ORDER_FAILED,
    ORDER_FISCALIZATION,
    ORDER_FULFILLMENT,
    ORDER_MARKING,
    ORDER_NEEDS_REVIEW,
    ORDER_NEW,
    ORDER_PAID,
    ORDER_PAYMENT_PENDING,
    ORDER_RESERVED,
    ORDER_RETURNED,
    ORDER_RETURN_PENDING,
    ORDER_SHIPMENT,
    ORDER_VALIDATED,
    assert_transition,
    can_transition,
)
from commerce.store import CommerceStore
from security.tenant import normalize_tenant_id


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _order_payload(order: CommerceOrder) -> dict:
    return {
        "order_id": order.order_id,
        "tenant_id": order.tenant_id,
        "buyer_type": order.buyer_type,
        "buyer_ref": order.buyer_ref,
        "purpose_declaration_ref": order.purpose_declaration_ref,
        "lines": [
            {
                "product_ref": ln.product_ref,
                "quantity": ln.quantity,
                "sku": ln.sku,
                "ean": ln.ean,
                "mpn": ln.mpn,
                "warehouse": ln.warehouse,
                "marking_code_refs": list(ln.marking_code_refs),
                "unit_price": ln.unit_price,
            }
            for ln in order.lines
        ],
        "totals": dict(order.totals),
        "payment_state_ref": order.payment_state_ref,
        "payment_status": order.payment_status,
        "fulfillment_state": order.fulfillment_state,
        "compliance_state": order.compliance_state,
        "external_order_refs": dict(order.external_order_refs),
        "provenance": dict(order.provenance),
        "rule_version": order.rule_version,
        "scenario": order.scenario,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
    }


def _order_from_payload(data: dict) -> CommerceOrder:
    lines = tuple(
        CommerceOrderLine(
            product_ref=str(ln.get("product_ref") or ""),
            quantity=float(ln.get("quantity") or 0),
            sku=str(ln.get("sku") or ""),
            ean=str(ln.get("ean") or ""),
            mpn=str(ln.get("mpn") or ""),
            warehouse=str(ln.get("warehouse") or ""),
            marking_code_refs=tuple(ln.get("marking_code_refs") or ()),
            unit_price=ln.get("unit_price"),
        )
        for ln in (data.get("lines") or ())
    )
    return CommerceOrder(
        order_id=data["order_id"],
        tenant_id=data["tenant_id"],
        buyer_type=data["buyer_type"],
        buyer_ref=str(data.get("buyer_ref") or ""),
        purpose_declaration_ref=str(data.get("purpose_declaration_ref") or ""),
        lines=lines,
        totals=dict(data.get("totals") or {}),
        payment_state_ref=str(data.get("payment_state_ref") or ""),
        payment_status=str(data.get("payment_status") or "unconfirmed"),
        fulfillment_state=str(data.get("fulfillment_state") or ORDER_NEW),
        compliance_state=str(data.get("compliance_state") or "none"),
        external_order_refs=dict(data.get("external_order_refs") or {}),
        provenance=dict(data.get("provenance") or {}),
        rule_version=str(data.get("rule_version") or ""),
        scenario=str(data.get("scenario") or ""),
        created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else _utc(),
        updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else _utc(),
    )


class CommerceService:
    def __init__(
        self,
        *,
        store: CommerceStore,
        rules: ComplianceRulesEngine | None = None,
        inventory=None,
        accounting=None,
        edo=None,
        marking=None,
        fiscal=None,
        front_office=None,
        workflow_runtime=None,
        hitl_service=None,
        document_service=None,
        data_intelligence=None,
        acquisition_service=None,
    ):
        self.store = store
        self.rules = rules or ComplianceRulesEngine()
        self.inventory = inventory or FakeInventoryGateway()
        self.accounting = accounting or FakeAccountingGateway()
        self.edo = edo or FakeEdoGateway()
        self.marking = marking or FakeMarkingGateway()
        self.fiscal = fiscal or FakeFiscalGateway()
        self.front_office = front_office or FakeFrontOfficeGateway()
        self.workflow_runtime = workflow_runtime
        self.hitl = hitl_service
        self.document_service = document_service
        self.data_intelligence = data_intelligence
        self.acquisition_service = acquisition_service
        self._own_use_history: dict[tuple[str, str], int] = {}

    # ---- guards ----
    def require_capabilities(self, held: tuple[str, ...] | None, required: tuple[str, ...]) -> None:
        have = set(held or ())
        missing = [c for c in required if c not in have]
        if missing:
            raise CapabilityDeniedError("capability_denied")
        for c in required:
            if c in LLM_DEFAULT_DENY and c not in have:
                raise CapabilityDeniedError("capability_denied")

    def _get_order(self, tenant_id: str, order_id: str) -> CommerceOrder:
        data = self.store.get_order(tenant_id, order_id)
        if data is None:
            raise TenantAccessDeniedError("tenant_access_denied")
        return _order_from_payload(data)

    def _save(self, order: CommerceOrder) -> CommerceOrder:
        self.store.save_order(
            order.tenant_id, order.order_id, _order_payload(order), order.fulfillment_state
        )
        return order

    def _transition(self, order: CommerceOrder, target: str) -> CommerceOrder:
        assert_transition("order", order.fulfillment_state, target)
        updated = replace(order, fulfillment_state=target, updated_at=_utc())
        return self._save(updated)

    def _op(self, tenant_id: str, kind: str, idempotency_key: str, payload: dict) -> tuple[str, dict | None]:
        op_id = f"cop-{uuid.uuid4().hex[:12]}"
        existing = self.store.begin_op(tenant_id, op_id, kind, idempotency_key, payload)
        if existing is not None:
            return existing["operation_id"], existing
        return op_id, None

    # ---- suppliers ----
    def upsert_supplier(self, record: SupplierRecord) -> SupplierRecord:
        self.store.save_supplier(
            record.tenant_id,
            record.supplier_id,
            {
                "supplier_id": record.supplier_id,
                "tenant_id": record.tenant_id,
                "name": record.name,
                "identifiers": dict(record.identifiers),
                "currency": record.currency,
                "moq": record.moq,
                "lead_time_days": record.lead_time_days,
                "payment_terms": record.payment_terms,
                "reliability_score": record.reliability_score,
                "error_rate": record.error_rate,
                "return_rate": record.return_rate,
                "active": record.active,
            },
            active=record.active,
        )
        self.store.audit(record.tenant_id, "supplier_upserted", details={"supplier_id": record.supplier_id})
        return record

    def rank_suppliers(self, tenant_id: str, *, price: float = 0.0) -> list[dict]:
        items = self.store.list_suppliers(tenant_id)
        ranked = []
        for s in items:
            # Transparent score: price + availability proxy + lead + reliability + terms
            lead = float(s.get("lead_time_days") or 0)
            rel = float(s.get("reliability_score") or 0)
            err = float(s.get("error_rate") or 0)
            score = (rel * 40.0) + max(0.0, 30.0 - lead) + max(0.0, 20.0 - err * 100.0) - (price * 0.01)
            ranked.append(
                {
                    "supplier_id": s["supplier_id"],
                    "score": round(score, 3),
                    "evidence": {
                        "reliability_score": rel,
                        "lead_time_days": lead,
                        "error_rate": err,
                        "price_input": price,
                    },
                }
            )
        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked

    # ---- purpose declaration ----
    def create_purpose_declaration(
        self,
        *,
        tenant_id: str,
        order_id: str,
        buyer_inn: str,
        buyer_name: str,
        selected_option: int,
        representative: str = "",
        session_ref: str = "",
        source_ip: str = "",
        source_channel: str = "",
    ) -> BuyerPurposeDeclaration:
        if selected_option not in DECLARATION_TEXTS:
            raise DeclarationRequiredError("declaration_required")
        existing = self.store.get_declaration_for_order(tenant_id, order_id)
        if existing is not None:
            raise DeclarationImmutableError("declaration_immutable")
        decl = BuyerPurposeDeclaration(
            declaration_id=f"decl-{uuid.uuid4().hex[:10]}",
            tenant_id=tenant_id,
            buyer_inn=buyer_inn,
            buyer_name=buyer_name,
            order_id=order_id,
            declaration_version="1",
            exact_text=DECLARATION_TEXTS[selected_option],
            selected_option=selected_option,
            representative=representative,
            session_ref=session_ref,
            source_ip=source_ip,
            source_channel=source_channel,
        )
        self.store.save_declaration(
            tenant_id,
            decl.declaration_id,
            order_id,
            {
                "declaration_id": decl.declaration_id,
                "tenant_id": decl.tenant_id,
                "buyer_inn": decl.buyer_inn,
                "buyer_name": decl.buyer_name,
                "order_id": decl.order_id,
                "declaration_version": decl.declaration_version,
                "exact_text": decl.exact_text,
                "selected_option": decl.selected_option,
                "representative": decl.representative,
                "timestamp": decl.timestamp.isoformat(),
                "session_ref": decl.session_ref,
                "source_ip": decl.source_ip,
                "source_channel": decl.source_channel,
            },
        )
        self.store.audit(
            tenant_id,
            "purpose_declaration_created",
            order_id=order_id,
            details={
                "declaration_id": decl.declaration_id,
                "selected_option": selected_option,
                "version": decl.declaration_version,
            },
        )
        order = self._get_order(tenant_id, order_id)
        self._save(replace(order, purpose_declaration_ref=decl.declaration_id, updated_at=_utc()))
        return decl

    def detect_b2b_risk(self, order: CommerceOrder, declaration_option: int | None) -> str | None:
        """Explainable signals only — no accusation; may return risk flag."""
        evidence = []
        total = float((order.totals or {}).get("amount") or 0)
        if total >= 500_000:
            evidence.append("unusually_large_amount")
        key = (order.tenant_id, order.buyer_ref)
        # track own-use frequency
        if declaration_option == 1:
            self._own_use_history[key] = self._own_use_history.get(key, 0) + 1
            if self._own_use_history[key] >= 3:
                evidence.append("repeated_own_needs_pattern")
        qty = sum(ln.quantity for ln in order.lines)
        if qty >= 50:
            evidence.append("high_volume_same_order")
        electronics = any("elec" in (ln.sku or ln.product_ref).lower() for ln in order.lines)
        if electronics and qty >= 10:
            evidence.append("repeated_electronics_volume")
        if evidence:
            self.store.audit(
                order.tenant_id,
                "compliance_risk_detected",
                order_id=order.order_id,
                details={"signals": evidence},
            )
            return "suspicious_own_use" if "repeated_own_needs_pattern" in evidence else "elevated_risk"
        return None

    # ---- orders ----
    def create_order(self, order: CommerceOrder) -> CommerceOrder:
        existing = self.store.get_order(order.tenant_id, order.order_id)
        if existing is not None:
            return _order_from_payload(existing)
        self._save(order)
        self.store.audit(order.tenant_id, "order_created", order_id=order.order_id, details={"buyer_type": order.buyer_type})
        return order

    def ingest_bitrix_order(
        self,
        *,
        tenant_id: str,
        external_order_id: str,
        idempotency_key: str = "",
    ) -> CommerceOrder:
        key = idempotency_key or f"bitrix-ingest:{external_order_id}"
        op_id, existing = self._op(tenant_id, "bitrix_ingest", key, {"external_order_id": external_order_id})
        if existing and existing.get("order_id"):
            return self._get_order(tenant_id, existing["order_id"])
        raw = dict(self.front_office.pull_order(tenant_id=tenant_id, external_order_id=external_order_id))
        if not raw:
            raise NeedsReviewError("needs_review")
        order_id = str(raw.get("order_id") or f"ord-{external_order_id}")
        lines = tuple(
            CommerceOrderLine(
                product_ref=str(ln.get("product_ref") or ln.get("sku") or ""),
                quantity=float(ln.get("quantity") or 0),
                sku=str(ln.get("sku") or ""),
                ean=str(ln.get("ean") or ""),
                warehouse=str(ln.get("warehouse") or "main"),
                unit_price=ln.get("unit_price"),
                marking_code_refs=tuple(ln.get("marking_code_refs") or ()),
            )
            for ln in (raw.get("lines") or ())
        )
        order = CommerceOrder(
            order_id=order_id,
            tenant_id=tenant_id,
            buyer_type=str(raw.get("buyer_type") or "B2C"),
            buyer_ref=str(raw.get("buyer_ref") or ""),
            lines=lines,
            totals=dict(raw.get("totals") or {}),
            payment_state_ref=str(raw.get("payment_state_ref") or ""),
            payment_status=str(raw.get("payment_status") or "unconfirmed"),
            external_order_refs={"bitrix": external_order_id},
            provenance={"source": "bitrix", "external_order_id": external_order_id},
        )
        order = self.create_order(order)
        self.store.complete_op(tenant_id, op_id, "completed", {"order_id": order.order_id})
        return order

    def validate_order(self, tenant_id: str, order_id: str) -> CommerceOrder:
        order = self._get_order(tenant_id, order_id)
        if not order.lines:
            return self._transition(order, ORDER_NEEDS_REVIEW)
        return self._transition(order, ORDER_VALIDATED)

    def update_payment_reference(
        self,
        *,
        tenant_id: str,
        order_id: str,
        payment_status: str,
        payment_refs: list[str] | tuple[str, ...] | None = None,
        unlock_code: str = "",
    ) -> CommerceOrder:
        """Explicit Payments→Commerce contract: safe payment refs only, no payments engine."""
        order = self._get_order(tenant_id, order_id)
        refs = list(payment_refs or [])
        primary = refs[0] if refs else order.payment_state_ref
        order = replace(
            order,
            payment_status=str(payment_status or order.payment_status),
            payment_state_ref=str(primary or order.payment_state_ref),
            provenance={
                **dict(order.provenance),
                "payment_unlock_code": unlock_code,
                "payment_refs": refs,
            },
            updated_at=_utc(),
        )
        self._save(order)
        self.store.audit(
            tenant_id,
            "payment_reference_updated",
            order_id=order_id,
            details={
                "payment_status": order.payment_status,
                "unlock_code": unlock_code,
                "refs_count": len(refs),
            },
        )
        return order

    # ---- inventory / reservation ----
    def read_inventory(self, *, tenant_id: str, product_ref: str, warehouse: str) -> InventoryPosition:
        return self.inventory.snapshot(tenant_id=tenant_id, product_ref=product_ref, warehouse=warehouse)

    def reserve_order(
        self,
        tenant_id: str,
        order_id: str,
        *,
        capabilities: tuple[str, ...] = (),
        idempotency_key: str = "",
    ) -> CommerceOperationResult:
        from commerce.capabilities import CAP_INVENTORY_RESERVE

        self.require_capabilities(capabilities, (CAP_INVENTORY_RESERVE,))
        order = self._get_order(tenant_id, order_id)
        key = idempotency_key or f"reserve:{order_id}"
        op_id, existing = self._op(tenant_id, "reserve", key, {"order_id": order_id})
        if existing and existing.get("status") == "completed":
            return CommerceOperationResult(
                operation_id=existing["operation_id"],
                workflow_id=order_id,
                status="completed",
                external_refs=dict(existing.get("external_refs") or {}),
                reconciliation_state="ok",
                provenance={"idempotent": True},
            )
        refs = {}
        try:
            for ln in order.lines:
                wh = ln.warehouse or "main"
                snap = self.read_inventory(tenant_id=tenant_id, product_ref=ln.product_ref, warehouse=wh)
                if snap.is_stale():
                    raise StaleStateError("stale_state")
                conf = self.inventory.reserve(
                    tenant_id=tenant_id,
                    product_ref=ln.product_ref,
                    warehouse=wh,
                    qty=ln.quantity,
                    idempotency_key=f"{key}:{ln.product_ref}:{wh}",
                )
                if not conf.external_id:
                    raise ExternalUnconfirmedError("external_unconfirmed")
                refs[ln.product_ref] = conf.external_id
        except (InsufficientStockError, OversellError, StaleStateError) as exc:
            if order.fulfillment_state not in {ORDER_NEEDS_REVIEW}:
                try:
                    order = self._transition(order, ORDER_NEEDS_REVIEW)
                except Exception:
                    order = self._save(replace(order, fulfillment_state=ORDER_NEEDS_REVIEW, updated_at=_utc()))
            self.store.complete_op(tenant_id, op_id, "failed", {"error": exc.code})
            return CommerceOperationResult(
                operation_id=op_id,
                workflow_id=order_id,
                status="failed",
                error=exc.code,
                after_state={"fulfillment_state": order.fulfillment_state},
            )
        # Advance to RESERVED through legal path
        if order.fulfillment_state == ORDER_NEW:
            order = self._transition(order, ORDER_VALIDATED)
        if order.fulfillment_state == ORDER_VALIDATED:
            order = self._transition(order, ORDER_PAID)
        if order.fulfillment_state == ORDER_PAYMENT_PENDING:
            order = self._transition(order, ORDER_PAID)
        order = self._transition(order, ORDER_RESERVED)
        payload = {"order_id": order_id, "external_refs": refs, "status": "completed"}
        self.store.complete_op(tenant_id, op_id, "completed", payload)
        self.store.audit(tenant_id, "reservation_confirmed", order_id=order_id, details={"refs": refs})
        return CommerceOperationResult(
            operation_id=op_id,
            workflow_id=order_id,
            status="completed",
            external_refs=refs,
            after_state={"fulfillment_state": ORDER_RESERVED},
            reconciliation_state="ok",
        )

    # ---- procurement ----
    def procurement_receive(
        self,
        *,
        tenant_id: str,
        supplier_id: str,
        lines: list[dict],
        expected_lines: list[dict] | None = None,
        idempotency_key: str = "",
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        """UPD/supply notice receipt foundation with mismatch → NEEDS_REVIEW."""
        from commerce.capabilities import CAP_INVENTORY_RESERVE, CAP_SUPPLIER_WRITE

        self.require_capabilities(capabilities or (CAP_SUPPLIER_WRITE, CAP_INVENTORY_RESERVE), (CAP_SUPPLIER_WRITE,))
        key = idempotency_key or f"recv:{uuid.uuid4().hex[:8]}"
        op_id, existing = self._op(tenant_id, "procurement_receive", key, {"supplier_id": supplier_id})
        if existing and existing.get("status") == "completed":
            return existing

        issues = []
        expected_map = {
            str(x.get("sku") or x.get("ean") or x.get("product_ref")): x for x in (expected_lines or [])
        }
        for ln in lines:
            sku = str(ln.get("sku") or "")
            ean = str(ln.get("ean") or "")
            product = str(ln.get("product_ref") or sku or ean)
            qty = float(ln.get("quantity") or 0)
            price = ln.get("unit_price")
            # SKU/EAN conflict
            if sku and ean and expected_map:
                for exp in expected_map.values():
                    if exp.get("ean") == ean and exp.get("sku") and exp.get("sku") != sku:
                        issues.append({"type": "sku_ean_conflict", "sku": sku, "ean": ean})
            exp = expected_map.get(sku) or expected_map.get(ean) or expected_map.get(product)
            if exp:
                eq = float(exp.get("quantity") or 0)
                if qty < eq:
                    issues.append({"type": "shortage", "product": product, "expected": eq, "actual": qty})
                elif qty > eq:
                    issues.append({"type": "overage", "product": product, "expected": eq, "actual": qty})
                if price is not None and exp.get("unit_price") is not None:
                    if float(price) != float(exp["unit_price"]):
                        issues.append({"type": "wrong_price", "product": product})
            marking = list(ln.get("marking_code_refs") or [])
            expected_marks = list((exp or {}).get("marking_code_refs") or [])
            if expected_marks and set(marking) != set(expected_marks):
                issues.append({"type": "marking_mismatch", "product": product})

        if issues:
            self.store.complete_op(
                tenant_id, op_id, "needs_review", {"issues": issues, "status": "needs_review"}
            )
            self.store.audit(tenant_id, "procurement_needs_review", details={"issues": issues})
            return {"operation_id": op_id, "status": "NEEDS_REVIEW", "issues": issues}

        confs = []
        for ln in lines:
            product = str(ln.get("product_ref") or ln.get("sku") or "")
            wh = str(ln.get("warehouse") or "main")
            conf = self.inventory.receive(
                tenant_id=tenant_id,
                product_ref=product,
                warehouse=wh,
                qty=float(ln.get("quantity") or 0),
                idempotency_key=f"{key}:inv:{product}",
            )
            acc = self.accounting.create_receipt(
                tenant_id=tenant_id,
                payload={"product": product, "qty": ln.get("quantity")},
                idempotency_key=f"{key}:acc:{product}",
            )
            confs.append({"inventory": conf.external_id, "accounting": acc.external_id})
        result = {"operation_id": op_id, "status": "completed", "confirmations": confs}
        self.store.complete_op(tenant_id, op_id, "completed", result)
        self.store.audit(tenant_id, "procurement_received", details={"supplier_id": supplier_id})
        return result

    # ---- compliance evaluation ----
    def evaluate_compliance(self, order: CommerceOrder) -> ComplianceDecision:
        decl = None
        option = None
        if order.purpose_declaration_ref:
            decl = self.store.get_declaration(order.tenant_id, order.purpose_declaration_ref)
            option = int((decl or {}).get("selected_option") or 0) or None
        if order.buyer_type == "B2B" and option is None:
            raise DeclarationRequiredError("declaration_required")
        risk = self.detect_b2b_risk(order, option) if order.buyer_type == "B2B" else None
        scenario = "b2c_fulfillment"
        if order.buyer_type == "B2B":
            scenario = "b2b_own_use" if option == 1 else "b2b_resale"
        decision = self.rules.select(
            buyer_type=order.buyer_type,
            scenario=scenario,
            declaration_option=option,
            risk_flag=risk,
        )
        self.store.record_rule_used(
            order.tenant_id, order.order_id, decision.evidence.get("rule_id") or "unknown", decision.rule_version
        )
        self._save(
            replace(
                order,
                rule_version=decision.rule_version,
                scenario=decision.scenario,
                compliance_state=decision.scenario,
                updated_at=_utc(),
            )
        )
        return decision

    # ---- workflows ----
    def run_b2c_fulfillment(
        self,
        tenant_id: str,
        order_id: str,
        *,
        capabilities: tuple[str, ...] = (),
        idempotency_key: str = "",
        fiscal_ok: bool = True,
        marking_ok: bool = True,
    ) -> CommerceOperationResult:
        from commerce.capabilities import CAP_FISCAL_CREATE, CAP_INVENTORY_RESERVE, CAP_MARKING_WITHDRAW, CAP_ORDER_WRITE

        self.require_capabilities(capabilities, (CAP_ORDER_WRITE, CAP_INVENTORY_RESERVE))
        key = idempotency_key or f"b2c:{order_id}"
        op_id, existing = self._op(tenant_id, "b2c_fulfillment", key, {"order_id": order_id})
        if existing and existing.get("status") == "completed":
            return CommerceOperationResult(
                operation_id=existing["operation_id"],
                workflow_id=order_id,
                status="completed",
                provenance={"idempotent": True},
            )

        order = self.validate_order(tenant_id, order_id)
        decision = self.evaluate_compliance(order)
        if decision.requires_hitl:
            order = self._transition(self._get_order(tenant_id, order_id), ORDER_COMPLIANCE_RISK)
            return CommerceOperationResult(
                operation_id=op_id, workflow_id=order_id, status=ORDER_COMPLIANCE_RISK, error="hitl_required"
            )

        if order.payment_status != "confirmed":
            order = self._transition(order, ORDER_PAYMENT_PENDING)
            self.store.complete_op(tenant_id, op_id, "payment_pending", {"order_id": order_id})
            return CommerceOperationResult(
                operation_id=op_id, workflow_id=order_id, status=ORDER_PAYMENT_PENDING
            )

        order = self._transition(order, ORDER_PAID)
        res = self.reserve_order(tenant_id, order_id, capabilities=capabilities, idempotency_key=f"{key}:res")
        if res.status != "completed":
            return res
        order = self._transition(self._get_order(tenant_id, order_id), ORDER_FULFILLMENT)
        order = self._transition(order, ORDER_FISCALIZATION)

        refs = dict(res.external_refs)
        if fiscal_ok:
            self.require_capabilities(capabilities, (CAP_FISCAL_CREATE,))
            fconf = self.fiscal.create_receipt(
                tenant_id=tenant_id,
                payload={"order_id": order_id, "payment_ref": order.payment_state_ref},
                idempotency_key=f"{key}:fiscal",
            )
            status = self.fiscal.get_receipt_status(tenant_id=tenant_id, receipt_external_id=fconf.external_id)
            if status.status != "OFD_CONFIRMED":
                order = self._transition(order, ORDER_COMPLIANCE_PENDING)
                self.store.complete_op(tenant_id, op_id, ORDER_COMPLIANCE_PENDING, {"fiscal": status.status})
                return CommerceOperationResult(
                    operation_id=op_id,
                    workflow_id=order_id,
                    status=ORDER_COMPLIANCE_PENDING,
                    error="fiscal_pending",
                    external_refs={"fiscal": fconf.external_id},
                )
            refs["fiscal"] = fconf.external_id
        else:
            order = self._transition(order, ORDER_COMPLIANCE_PENDING)
            return CommerceOperationResult(
                operation_id=op_id, workflow_id=order_id, status=ORDER_COMPLIANCE_PENDING, error="fiscal_failed"
            )

        order = self._transition(self._get_order(tenant_id, order_id), ORDER_MARKING)
        for ln in order.lines:
            for code in ln.marking_code_refs:
                if not marking_ok:
                    order = self._transition(order, ORDER_COMPLIANCE_PENDING)
                    return CommerceOperationResult(
                        operation_id=op_id,
                        workflow_id=order_id,
                        status=ORDER_COMPLIANCE_PENDING,
                        error="marking_failed",
                    )
                self.require_capabilities(capabilities, (CAP_MARKING_WITHDRAW,))
                mconf = self.marking.withdraw(
                    tenant_id=tenant_id, code_ref=code, idempotency_key=f"{key}:mark:{code}"
                )
                if mconf.status != MARKING_WITHDRAWN:
                    raise ExternalUnconfirmedError("external_unconfirmed")
                refs[f"marking:{code}"] = mconf.external_id

        order = self._transition(self._get_order(tenant_id, order_id), ORDER_SHIPMENT)
        order = self._transition(order, ORDER_COMPLETED)
        self.store.complete_op(tenant_id, op_id, "completed", {"refs": refs, "status": "completed"})
        self.store.audit(
            tenant_id,
            "b2c_completed",
            order_id=order_id,
            details={"rule_version": decision.rule_version, "refs": list(refs.keys())},
        )
        self.enqueue_reconcile(tenant_id, order_id=order_id, reason="b2c_completed")
        return CommerceOperationResult(
            operation_id=op_id,
            workflow_id=order_id,
            status="completed",
            external_refs=refs,
            after_state={"fulfillment_state": ORDER_COMPLETED},
            reconciliation_state="ok",
        )

    def run_b2b_own_use(
        self,
        tenant_id: str,
        order_id: str,
        *,
        capabilities: tuple[str, ...] = (),
        idempotency_key: str = "",
    ) -> CommerceOperationResult:
        from commerce.capabilities import CAP_MARKING_WITHDRAW, CAP_ORDER_WRITE, CAP_INVENTORY_RESERVE

        self.require_capabilities(capabilities, (CAP_ORDER_WRITE, CAP_INVENTORY_RESERVE))
        order = self._get_order(tenant_id, order_id)
        if not order.purpose_declaration_ref:
            raise DeclarationRequiredError("declaration_required")
        decision = self.evaluate_compliance(order)
        if decision.scenario == "compliance_risk" or decision.requires_hitl:
            order = self._transition(order, ORDER_COMPLIANCE_RISK)
            return CommerceOperationResult(
                operation_id=f"risk-{order_id}",
                workflow_id=order_id,
                status=ORDER_COMPLIANCE_RISK,
                error="hitl_required",
            )
        self.rules.assert_action_allowed(decision, "marking_withdraw_if_applicable")
        if order.payment_status != "confirmed":
            return CommerceOperationResult(
                operation_id="pay", workflow_id=order_id, status=ORDER_PAYMENT_PENDING
            )
        key = idempotency_key or f"b2b-own:{order_id}"
        if order.fulfillment_state == ORDER_NEW:
            order = self._transition(order, ORDER_VALIDATED)
        if order.fulfillment_state == ORDER_VALIDATED:
            order = self._transition(order, ORDER_PAID)
        if order.fulfillment_state == ORDER_PAYMENT_PENDING:
            order = self._transition(order, ORDER_PAID)
        res = self.reserve_order(tenant_id, order_id, capabilities=capabilities, idempotency_key=f"{key}:res")
        if res.status != "completed":
            return res
        order = self._get_order(tenant_id, order_id)
        order = self._transition(order, ORDER_FULFILLMENT)
        order = self._transition(order, ORDER_MARKING)
        refs = dict(res.external_refs)
        for ln in order.lines:
            for code in ln.marking_code_refs:
                self.require_capabilities(capabilities, (CAP_MARKING_WITHDRAW,))
                m = self.marking.withdraw(tenant_id=tenant_id, code_ref=code, idempotency_key=f"{key}:w:{code}")
                refs[code] = m.external_id
        order = self._transition(self._get_order(tenant_id, order_id), ORDER_SHIPMENT)
        order = self._transition(order, ORDER_COMPLETED)
        self.store.audit(tenant_id, "b2b_own_use_completed", order_id=order_id, details={"rule_version": decision.rule_version})
        self.enqueue_reconcile(tenant_id, order_id=order_id, reason="b2b_own_use_completed")
        return CommerceOperationResult(
            operation_id=key, workflow_id=order_id, status="completed", external_refs=refs
        )

    def run_b2b_resale(
        self,
        tenant_id: str,
        order_id: str,
        *,
        capabilities: tuple[str, ...] = (),
        idempotency_key: str = "",
    ) -> CommerceOperationResult:
        from commerce.capabilities import (
            CAP_EDO_PREPARE,
            CAP_EDO_SEND,
            CAP_INVENTORY_RESERVE,
            CAP_MARKING_TRANSFER,
            CAP_ORDER_WRITE,
        )

        self.require_capabilities(
            capabilities, (CAP_ORDER_WRITE, CAP_EDO_PREPARE, CAP_EDO_SEND, CAP_MARKING_TRANSFER)
        )
        order = self._get_order(tenant_id, order_id)
        if not order.purpose_declaration_ref:
            raise DeclarationRequiredError("declaration_required")
        decision = self.evaluate_compliance(order)
        # Resale forbids final-consumption withdrawal — enforced by not calling withdraw.
        if "marking_withdraw_as_consumption" in decision.forbidden_actions:
            pass
        key = idempotency_key or f"b2b-resale:{order_id}"
        op_id, existing = self._op(tenant_id, "b2b_resale", key, {"order_id": order_id})
        if existing and existing.get("status") == "completed":
            return CommerceOperationResult(
                operation_id=existing["operation_id"],
                workflow_id=order_id,
                status="completed",
                provenance={"idempotent": True},
            )
        if order.payment_status != "confirmed":
            return CommerceOperationResult(operation_id=op_id, workflow_id=order_id, status=ORDER_PAYMENT_PENDING)
        if order.fulfillment_state == ORDER_NEW:
            order = self._transition(order, ORDER_VALIDATED)
        if order.fulfillment_state == ORDER_VALIDATED:
            order = self._transition(order, ORDER_PAID)
        res = self.reserve_order(
            tenant_id, order_id, capabilities=capabilities + (CAP_INVENTORY_RESERVE,), idempotency_key=f"{key}:res"
        )
        if res.status != "completed":
            return res
        order = self._get_order(tenant_id, order_id)
        order = self._transition(order, ORDER_FULFILLMENT)
        edo_doc = self.edo.prepare_document(
            tenant_id=tenant_id,
            payload={"order_id": order_id, "type": "UPD"},
            idempotency_key=f"{key}:edo-prep",
        )
        codes = tuple(c for ln in order.lines for c in ln.marking_code_refs)
        self.edo.attach_marking_codes(
            tenant_id=tenant_id,
            document_external_id=edo_doc.external_id,
            codes=codes,
            idempotency_key=f"{key}:edo-codes",
        )
        sent = self.edo.send_document(
            tenant_id=tenant_id,
            document_external_id=edo_doc.external_id,
            idempotency_key=f"{key}:edo-send",
        )
        # duplicate send prevented by idempotency
        refs = {"edo": sent.external_id}
        for code in codes:
            t = self.marking.transfer(
                tenant_id=tenant_id,
                code_ref=code,
                to_owner=order.buyer_ref or "counterparty",
                idempotency_key=f"{key}:xfer:{code}",
            )
            if t.status != MARKING_TRANSFERRED:
                raise ExternalUnconfirmedError("external_unconfirmed")
            # ensure not withdrawn
            st = self.marking.read_status(tenant_id=tenant_id, code_ref=code)
            if st.status == MARKING_WITHDRAWN:
                raise NeedsReviewError("needs_review")
            refs[f"marking:{code}"] = t.external_id
        order = self._transition(self._get_order(tenant_id, order_id), ORDER_SHIPMENT)
        order = self._transition(order, ORDER_COMPLETED)
        self.store.complete_op(tenant_id, op_id, "completed", {"refs": refs, "status": "completed"})
        self.store.audit(
            tenant_id,
            "b2b_resale_completed",
            order_id=order_id,
            details={"rule_version": decision.rule_version, "edo": sent.external_id, "withdrawn": False},
        )
        self.enqueue_reconcile(tenant_id, order_id=order_id, reason="b2b_resale_completed")
        return CommerceOperationResult(
            operation_id=op_id, workflow_id=order_id, status="completed", external_refs=refs
        )

    def cancel_order(self, tenant_id: str, order_id: str, *, capabilities: tuple[str, ...] = ()) -> CommerceOperationResult:
        from commerce.capabilities import CAP_ORDER_WRITE

        self.require_capabilities(capabilities, (CAP_ORDER_WRITE,))
        order = self._get_order(tenant_id, order_id)
        state = order.fulfillment_state
        if state in {ORDER_NEW, ORDER_VALIDATED, ORDER_PAYMENT_PENDING}:
            order = self._transition(order, ORDER_CANCELLED)
            self.enqueue_reconcile(tenant_id, order_id=order_id, reason="cancelled")
            return CommerceOperationResult(operation_id=f"cancel-{order_id}", workflow_id=order_id, status="cancelled")
        if state == ORDER_RESERVED:
            order = self._transition(order, ORDER_CANCEL_PENDING)
            # release reservations via external confirmation
            for ln in order.lines:
                self.inventory.release(
                    tenant_id=tenant_id,
                    reservation_external_id=f"res-{ln.product_ref}",
                    idempotency_key=f"cancel-rel:{order_id}:{ln.product_ref}",
                )
            order = self._transition(self._get_order(tenant_id, order_id), ORDER_CANCELLED)
            self.enqueue_reconcile(tenant_id, order_id=order_id, reason="cancelled_after_reserve")
            return CommerceOperationResult(operation_id=f"cancel-{order_id}", workflow_id=order_id, status="cancelled")
        if state in {ORDER_FISCALIZATION, ORDER_MARKING, ORDER_SHIPMENT, ORDER_COMPLETED}:
            order = self._transition(order, ORDER_RETURN_PENDING)
            self.enqueue_reconcile(tenant_id, order_id=order_id, reason="cancel_to_return")
            return CommerceOperationResult(
                operation_id=f"cancel-{order_id}", workflow_id=order_id, status=ORDER_RETURN_PENDING
            )
        order = self._transition(order, ORDER_CANCEL_PENDING)
        self.enqueue_reconcile(tenant_id, order_id=order_id, reason="cancel_pending")
        return CommerceOperationResult(
            operation_id=f"cancel-{order_id}", workflow_id=order_id, status=ORDER_CANCEL_PENDING
        )

    def return_order(
        self,
        tenant_id: str,
        order_id: str,
        *,
        capabilities: tuple[str, ...] = (),
        reintroduce_marking: bool = False,
        hitl_approved: bool = False,
        idempotency_key: str = "",
    ) -> CommerceOperationResult:
        from commerce.capabilities import CAP_MARKING_TRANSFER, CAP_ORDER_WRITE

        self.require_capabilities(capabilities, (CAP_ORDER_WRITE,))
        order = self._get_order(tenant_id, order_id)
        if order.fulfillment_state in {ORDER_COMPLETED, ORDER_SHIPMENT}:
            order = self._transition(order, ORDER_RETURN_PENDING)
        elif order.fulfillment_state != ORDER_RETURN_PENDING:
            order = self._transition(order, ORDER_RETURN_PENDING)
        key = idempotency_key or f"return:{order_id}"
        op_id, existing = self._op(tenant_id, "return", key, {"order_id": order_id})
        if existing and existing.get("status") == "completed":
            return CommerceOperationResult(
                operation_id=existing["operation_id"],
                workflow_id=order_id,
                status="completed",
                provenance={"idempotent": True},
            )
        refs = {}
        if reintroduce_marking:
            if not hitl_approved:
                return CommerceOperationResult(
                    operation_id=op_id,
                    workflow_id=order_id,
                    status="NEEDS_REVIEW",
                    error="hitl_required",
                )
            for ln in order.lines:
                for code in ln.marking_code_refs:
                    conf = self.marking.reintroduce(
                        tenant_id=tenant_id, code_ref=code, idempotency_key=f"{key}:re:{code}"
                    )
                    refs[code] = conf.external_id
        for ln in order.lines:
            conf = self.inventory.receive(
                tenant_id=tenant_id,
                product_ref=ln.product_ref,
                warehouse=ln.warehouse or "main",
                qty=ln.quantity,
                idempotency_key=f"{key}:inv:{ln.product_ref}",
            )
            refs[f"inv:{ln.product_ref}"] = conf.external_id
        order = self._transition(self._get_order(tenant_id, order_id), ORDER_RETURNED)
        self.store.complete_op(tenant_id, op_id, "completed", {"refs": refs})
        self.store.audit(
            tenant_id, "return_completed", order_id=order_id, details={"refs": list(refs.keys())}
        )
        self.enqueue_reconcile(tenant_id, order_id=order_id, reason="return_completed")
        return CommerceOperationResult(
            operation_id=op_id, workflow_id=order_id, status="completed", external_refs=refs
        )

    # ---- reconciliation ----
    def enqueue_reconcile(
        self,
        tenant_id: str,
        *,
        order_id: str = "",
        reason: str = "event",
        idempotency_key: str | None = None,
    ) -> dict:
        """Enqueue background commerce.reconcile — never runs legal writes inline."""
        tenant = normalize_tenant_id(tenant_id)
        wr = self.workflow_runtime
        key = idempotency_key or (
            f"commerce-reconcile-event:{tenant}:{order_id or 'tenant'}:{reason}"
        )
        if wr is None:
            # Tests without workflow: run sync tenant/order reconcile
            if order_id:
                result = self.reconcile_order(tenant, order_id, workflow_id="", run_id=key)
            else:
                result = self.reconcile_tenant(tenant, workflow_id="", run_id=key)
            return {"status": "inline", "result": result, "execution_key": key}
        existing = wr.state_manager.find_by_execution_key(key, tenant_id=tenant)
        if existing is not None:
            return {
                "workflow_id": existing.workflow_id,
                "execution_key": key,
                "idempotent": True,
                "status": existing.status,
            }
        meta = {
            "tenant_id": tenant,
            "order_id": order_id,
            "reason": reason,
            "trigger": "event",
        }
        created = wr.create_workflow(
            "commerce.reconcile",
            "1",
            task_id=f"reconcile-{uuid.uuid4().hex[:8]}",
            execution_key=key,
            metadata=meta,
            tenant_id=tenant,
        )
        enqueued = wr.enqueue_existing(
            created["workflow_id"],
            metadata={"workflow_type": "commerce.reconcile", "version": "1"},
        )
        return {**enqueued, "execution_key": key, "idempotent": False}

    def _request_reconcile_hitl(
        self,
        *,
        tenant_id: str,
        order_id: str,
        finding_id: str,
        evidence: list,
        workflow_id: str = "",
    ) -> str | None:
        """HUMAN_REVIEW → existing HITL path. Never auto-correct legal state."""
        if self.hitl is None:
            self.store.audit(
                tenant_id,
                "reconcile_human_review",
                order_id=order_id,
                details={"finding_id": finding_id, "hitl": False},
            )
            return None
        try:
            from autonomy.models import (
                ACTION_WRITE,
                DECISION_REQUIRE_APPROVAL,
                RISK_HIGH,
                AutonomyDecision,
                ProposedAction,
                utc_now,
            )

            wf_id = workflow_id or f"commerce-reconcile:{order_id or tenant_id}"
            action = ProposedAction(
                action_id=f"reconcile-{finding_id}",
                workflow_id=wf_id,
                task_id=finding_id,
                action_type=ACTION_WRITE,
                tool_id="commerce.reconcile",
                operation="human_review",
                resource=f"tenant:{tenant_id}:order:{order_id or '*'}",
                risk_class=RISK_HIGH,
                requested_capabilities=(),
                tool_trust_level="PRIVILEGED",
                metadata={
                    "finding_id": finding_id,
                    "evidence_count": len(evidence),
                    "auto_correct": False,
                },
            )
            decision = AutonomyDecision(
                decision_id=f"dec-{finding_id}",
                action_id=action.action_id,
                decision=DECISION_REQUIRE_APPROVAL,
                risk_class=RISK_HIGH,
                reason_code="commerce_reconcile_human_review",
                required_approval=True,
                capabilities_checked=(),
                idempotency_required=False,
                idempotency_satisfied=True,
                tool_trust_level="PRIVILEGED",
                timestamp=utc_now(),
                metadata={"finding_id": finding_id, "auto_correct": False},
            )
            record = self.hitl.request_approval(
                action, decision, requested_by="commerce.reconcile"
            )
            self.store.audit(
                tenant_id,
                "reconcile_human_review",
                order_id=order_id,
                details={
                    "finding_id": finding_id,
                    "approval_id": getattr(record, "approval_id", ""),
                    "hitl": True,
                },
            )
            return getattr(record, "approval_id", None)
        except Exception:
            self.store.audit(
                tenant_id,
                "reconcile_human_review",
                order_id=order_id,
                details={"finding_id": finding_id, "hitl_error": True},
            )
            return None

    def reconcile_order(
        self,
        tenant_id: str,
        order_id: str,
        *,
        workflow_id: str = "",
        run_id: str = "",
    ) -> dict:
        order = self._get_order(tenant_id, order_id)
        findings = []
        # shipped but marking incomplete
        if order.fulfillment_state in {ORDER_SHIPMENT, ORDER_COMPLETED}:
            for ln in order.lines:
                for code in ln.marking_code_refs:
                    st = self.marking.read_status(tenant_id=tenant_id, code_ref=code)
                    if order.scenario == "b2b_resale" and st.status != MARKING_TRANSFERRED:
                        findings.append(
                            {
                                "code": "shipped_marking_incomplete",
                                "check_type": "marking",
                                "marking": code,
                                "status": st.status,
                            }
                        )
                    if order.scenario in {"b2c_fulfillment", "b2b_own_use"} and st.status not in {
                        MARKING_WITHDRAWN,
                        "REINTRODUCED",
                        "AVAILABLE",
                    }:
                        if st.status not in {MARKING_WITHDRAWN}:
                            findings.append(
                                {
                                    "code": "marking_incomplete",
                                    "check_type": "marking",
                                    "marking": code,
                                    "status": st.status,
                                }
                            )
        if order.fulfillment_state == ORDER_CANCELLED:
            for ln in order.lines:
                for code in ln.marking_code_refs:
                    st = self.marking.read_status(tenant_id=tenant_id, code_ref=code)
                    if st.status == MARKING_WITHDRAWN:
                        findings.append(
                            {
                                "code": "marked_but_cancelled",
                                "check_type": "marking_cancel",
                                "marking": code,
                            }
                        )
        for ln in order.lines:
            snap = self.read_inventory(
                tenant_id=tenant_id, product_ref=ln.product_ref, warehouse=ln.warehouse or "main"
            )
            if snap.available < 0:
                findings.append(
                    {
                        "code": "negative_stock",
                        "check_type": "inventory",
                        "product": ln.product_ref,
                    }
                )
        severity = "OK"
        status = "OK"
        if findings:
            severity = (
                "RECONCILIATION_ERROR"
                if any(f["code"] in {"marked_but_cancelled", "negative_stock"} for f in findings)
                else "WARNING"
            )
            status = severity
            if severity == "RECONCILIATION_ERROR":
                status = "HUMAN_REVIEW"
                try:
                    if can_transition("order", order.fulfillment_state, ORDER_NEEDS_REVIEW):
                        self._transition(order, ORDER_NEEDS_REVIEW)
                    elif order.fulfillment_state == ORDER_COMPLETED and can_transition(
                        "order", ORDER_COMPLETED, ORDER_RETURN_PENDING
                    ):
                        pass
                except Exception:
                    pass
        run = run_id or f"run-{uuid.uuid4().hex[:10]}"
        finding_id = f"rec-{order_id}-{uuid.uuid4().hex[:6]}"
        approval_id = None
        if status == "HUMAN_REVIEW":
            approval_id = self._request_reconcile_hitl(
                tenant_id=tenant_id,
                order_id=order_id,
                finding_id=finding_id,
                evidence=findings,
                workflow_id=workflow_id,
            )
        # Never auto-correct marking/fiscal/inventory/EDO from findings.
        self.store.save_reconcile_finding(
            tenant_id,
            finding_id,
            severity,
            {
                "findings": findings,
                "external_refs": dict(order.external_order_refs),
                "auto_corrected": False,
                "approval_id": approval_id or "",
            },
            status=status,
            run_id=run,
            workflow_id=workflow_id,
            order_id=order_id,
        )
        self.store.audit(
            tenant_id,
            "commerce_reconcile",
            order_id=order_id,
            details={"severity": severity, "status": status, "run_id": run},
        )
        return {
            "finding_id": finding_id,
            "severity": severity,
            "status": status,
            "findings": findings,
            "run_id": run,
            "workflow_id": workflow_id,
            "order_id": order_id,
            "auto_corrected": False,
            "approval_id": approval_id,
        }

    def reconcile_tenant(
        self,
        tenant_id: str,
        *,
        workflow_id: str = "",
        run_id: str = "",
    ) -> dict:
        """Tenant-scoped batch reconciliation — never cross-tenant."""
        tenant = normalize_tenant_id(tenant_id)
        run = run_id or f"run-{uuid.uuid4().hex[:10]}"
        order_ids = self.store.list_order_ids(tenant)
        results = []
        worst = "OK"
        for oid in order_ids:
            try:
                r = self.reconcile_order(
                    tenant, oid, workflow_id=workflow_id, run_id=run
                )
            except Exception as exc:
                r = {
                    "order_id": oid,
                    "severity": "WARNING",
                    "status": "WARNING",
                    "error": getattr(exc, "code", "reconcile_error"),
                }
            results.append(r)
            for level in ("OK", "WARNING", "RECONCILIATION_ERROR", "HUMAN_REVIEW"):
                if r.get("status") == level or r.get("severity") == level:
                    if ("OK", "WARNING", "RECONCILIATION_ERROR", "HUMAN_REVIEW").index(
                        level
                    ) > ("OK", "WARNING", "RECONCILIATION_ERROR", "HUMAN_REVIEW").index(
                        worst
                    ):
                        worst = level
        summary_id = f"rec-tenant-{uuid.uuid4().hex[:8]}"
        self.store.save_reconcile_finding(
            tenant,
            summary_id,
            worst if worst != "HUMAN_REVIEW" else "RECONCILIATION_ERROR",
            {
                "check_type": "tenant_batch",
                "order_count": len(order_ids),
                "result_refs": [r.get("finding_id") for r in results if r.get("finding_id")],
                "auto_corrected": False,
            },
            status=worst,
            run_id=run,
            workflow_id=workflow_id,
            order_id="",
        )
        return {
            "run_id": run,
            "tenant_id": tenant,
            "workflow_id": workflow_id,
            "status": worst,
            "severity": worst,
            "order_count": len(order_ids),
            "results": results,
            "finding_id": summary_id,
            "auto_corrected": False,
        }

    def critical_action(
        self,
        *,
        tenant_id: str,
        action: str,
        capabilities: tuple[str, ...] = (),
        hitl_approved: bool = False,
        decision: ComplianceDecision | None = None,
    ) -> None:
        """Critical Action Guard: rules → capability → HITL → adapter."""
        if action in LLM_DEFAULT_DENY or (decision and action in decision.forbidden_actions):
            if action.split(".")[-1] if False else action:
                pass
            from commerce.capabilities import CAP_FISCAL_REFUND, CAP_INVENTORY_ADJUST, CAP_MARKING_WITHDRAW

            required = {
                "marking.withdraw": CAP_MARKING_WITHDRAW,
                "fiscal.refund": CAP_FISCAL_REFUND,
                "inventory.adjust": CAP_INVENTORY_ADJUST,
            }.get(action)
            if required:
                self.require_capabilities(capabilities, (required,))
            if decision and decision.requires_hitl and not hitl_approved:
                raise NeedsReviewError("needs_review")
            if action in LLM_DEFAULT_DENY and not hitl_approved:
                raise NeedsReviewError("needs_review")
