"""Block 20 — governed order ingest → Panda → 1C/CRM fixture plans. Offline only."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from commerce.capabilities import CAP_ORDER_WRITE  # ingest remains tenant-scoped; downstream uses ERP/CRM write caps
from data_intel.economics import EconomicsInput, calculate_economics
from governed_publish.contracts import (
    MODE_FIXTURE,
    MODE_LIVE,
    STATUS_ALREADY_EXECUTED,
    STATUS_APPROVAL_REQUIRED,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_EXECUTED_FIXTURE,
    STATUS_REJECTED,
    PublicationPlan,
    PublicationReceipt,
    idempotency_key,
    utc_now as pub_utc,
)
from governed_publish.governance import PublicationGovernance
from governed_publish.store import GovernedPublishStore
from security.tenant import require_tenant_id

from order_orchestration.adapters import FixtureCrmOrderAdapter, FixtureOneCOrderAdapter
from order_orchestration.contracts import (
    AGG_BLOCKED,
    AGG_COMPLETE,
    AGG_PARTIAL,
    AMBIGUOUS,
    CanonicalOrder,
    FULFILL_UNKNOWN,
    INGEST_CONFLICT,
    INGEST_NEW,
    INGEST_REPLAY,
    INGEST_UPDATED_VERSION,
    MAPPED,
    MISSING,
    OrderLine,
    PAY_UNKNOWN,
    SOURCE_SITE,
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RECEIVED,
    STATUS_REQUIRES_REVIEW,
    STATUS_VALIDATED,
    SUPPORTED_SOURCES,
    payload_hash,
    utc_now,
)
from order_orchestration.errors import (
    AMBIGUOUS_PRODUCT_MAPPING,
    APPROVAL_REJECTED,
    CAPABILITY_DENIED,
    CURRENCY_MISMATCH,
    INVALID_PRICE,
    INVALID_QUANTITY,
    LIVE_FORBIDDEN,
    MISSING_ORDER_ID,
    MISSING_PRODUCT_MAPPING,
    ONEC_BLOCKED,
    ORDER_ACCESS_DENIED,
    ORDER_CONFLICT,
    STALE_APPROVAL,
    STALE_ORDER_EVENT,
    UNSUPPORTED_SOURCE,
    OrderOrchError,
)
from order_orchestration.mapping import resolve_line
from order_orchestration.store import OrderStore

ONEC_WRITE_CAP = "erp.1c.catalog.write"
CRM_WRITE_CAP = "crm.contact.write"


def _dec(value) -> Decimal | None:
    if value is None or value == "" or str(value).strip().upper() == "UNKNOWN":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OrderOrchError("INVALID_PRICE") from None


def _normalize_status(source_status: str) -> str:
    raw = str(source_status or "").strip().upper()
    mapping = {
        "NEW": STATUS_RECEIVED,
        "RECEIVED": STATUS_RECEIVED,
        "CONFIRMED": STATUS_ACCEPTED,
        "VALIDATED": STATUS_VALIDATED,
        "CANCELLED": STATUS_CANCELLED,
        "CANCELED": STATUS_CANCELLED,
        "FULFILLED": "FULFILLED",
        "DELIVERED": "FULFILLED",
        "FAILED": STATUS_FAILED,
    }
    return mapping.get(raw, STATUS_RECEIVED)


class OrderOrchestrationService:
    def __init__(
        self,
        store: OrderStore | None = None,
        plans: GovernedPublishStore | None = None,
        governance: PublicationGovernance | None = None,
    ) -> None:
        self.store = store or OrderStore()
        self.plans = plans or GovernedPublishStore()
        self.governance = governance or PublicationGovernance()
        self.onec = FixtureOneCOrderAdapter()
        self.crm = FixtureCrmOrderAdapter()

    def _mode(self, mode: str) -> str:
        if (mode or MODE_FIXTURE).upper() == MODE_LIVE:
            raise OrderOrchError(LIVE_FORBIDDEN)
        return MODE_FIXTURE

    def _cap(self, capabilities, needed: str) -> None:
        if needed not in set(capabilities or ()):
            raise OrderOrchError(CAPABILITY_DENIED)

    def ingest(
        self,
        raw: dict,
        *,
        tenant_id: str,
        catalog: dict | None = None,
        mode: str = MODE_FIXTURE,
        economics: EconomicsInput | None = None,
    ) -> CanonicalOrder:
        tenant = require_tenant_id(tenant_id)
        self._mode(mode)
        source = str(raw.get("source") or SOURCE_SITE).upper()
        if source not in SUPPORTED_SOURCES:
            raise OrderOrchError(UNSUPPORTED_SOURCE)
        ext = str(raw.get("external_order_id") or raw.get("order_id") or "").strip()
        if not ext:
            raise OrderOrchError(MISSING_ORDER_ID)
        currency = str(raw.get("currency") or "RUB")
        lines_in = list(raw.get("lines") or raw.get("items") or [])
        if not lines_in:
            raise OrderOrchError("INVALID_ORDER", "no_lines")
        lines: list[OrderLine] = []
        for i, row in enumerate(lines_in):
            qty = _dec(row.get("quantity"))
            if qty is None or qty <= 0:
                raise OrderOrchError(INVALID_QUANTITY)
            price = _dec(row.get("unit_price") if "unit_price" in row else row.get("price"))
            if price is not None and price < 0:
                raise OrderOrchError(INVALID_PRICE)
            lc = str(row.get("currency") or currency)
            if lc != currency:
                raise OrderOrchError(CURRENCY_MISMATCH)
            lt = _dec(row.get("line_total"))
            line = OrderLine(
                line_id=str(row.get("line_id") or f"L{i+1}"),
                sku=str(row.get("sku") or ""),
                article=str(row.get("article") or ""),
                barcode=str(row.get("barcode") or ""),
                product_id=str(row.get("product_id") or ""),
                source_offer_id=str(row.get("offer_id") or ""),
                quantity=qty,
                unit_price=price,
                line_total=lt,
                currency=lc,
            )
            lines.append(resolve_line(line, catalog=catalog or {}))
        source_status = str(raw.get("source_status") or raw.get("status") or "NEW")
        canonical = _normalize_status(source_status)
        version = str(raw.get("source_order_version") or raw.get("version") or "1")
        try:
            ver_n = int(version)
        except ValueError:
            ver_n = 0
        raw_hash = payload_hash(
            {"source": source, "external_order_id": ext, "lines": lines_in, "source_status": source_status, "version": version}
        )
        idem = f"{tenant}:{source}:{ext}"
        existing = self.store.by_external(tenant_id=tenant, source=source, external_order_id=ext)
        if existing:
            if existing.raw_hash == raw_hash:
                self.store.audit(tenant, {"event": "ORDER_REPLAYED", **existing.public_audit_ref()})
                return replace(existing, ingest_result=INGEST_REPLAY)
            if ver_n and existing.event_seq and ver_n < existing.event_seq:
                raise OrderOrchError(STALE_ORDER_EVENT)
            if existing.cancelled and canonical != STATUS_CANCELLED:
                raise OrderOrchError(STALE_ORDER_EVENT)
            immutable_conflict = existing.currency != currency or tuple(
                (ln.sku, str(ln.unit_price), str(ln.quantity)) for ln in existing.lines
            ) != tuple((ln.sku, str(ln.unit_price), str(ln.quantity)) for ln in lines)
            if immutable_conflict and canonical != STATUS_CANCELLED:
                self.store.audit(tenant, {"event": "ORDER_CONFLICT", **existing.public_audit_ref()})
                raise OrderOrchError(ORDER_CONFLICT)
            if canonical == STATUS_CANCELLED:
                updated = replace(
                    existing,
                    canonical_status=STATUS_CANCELLED,
                    source_status=source_status,
                    cancelled=True,
                    ingest_result=INGEST_UPDATED_VERSION,
                    event_seq=max(existing.event_seq, ver_n),
                    raw_hash=raw_hash,
                    updated_at=utc_now(),
                )
                self.store.save(updated)
                self.store.audit(tenant, {"event": "ORDER_CANCELLED", **updated.public_audit_ref()})
                return updated
            updated = replace(
                existing,
                source_status=source_status,
                canonical_status=canonical,
                raw_hash=raw_hash,
                ingest_result=INGEST_UPDATED_VERSION,
                event_seq=max(existing.event_seq, ver_n),
                updated_at=utc_now(),
            )
            self.store.save(updated)
            self.store.audit(tenant, {"event": "ORDER_UPDATED", **updated.public_audit_ref()})
            return updated

        mapping_issues = [ln.mapping_status for ln in lines]
        if AMBIGUOUS in mapping_issues:
            validation = STATUS_REQUIRES_REVIEW
            ingest_issues = AMBIGUOUS_PRODUCT_MAPPING
        elif MISSING in mapping_issues:
            validation = STATUS_REQUIRES_REVIEW
            ingest_issues = MISSING_PRODUCT_MAPPING
        else:
            validation = STATUS_VALIDATED
            ingest_issues = ""
        if canonical == STATUS_CANCELLED:
            validation = STATUS_CANCELLED

        eco = {}
        if economics is not None:
            calc = calculate_economics(economics)
            eco = {
                "engine": "data_intel.economics",
                "decision": calc.get("decision"),
                "completeness": calc.get("completeness"),
                "note": calc.get("note"),
                "contribution_estimate": calc.get("contribution") if calc.get("completeness") == "COMPLETE" else None,
            }

        order_total = _dec(raw.get("order_total")) if "order_total" in raw else None
        order = CanonicalOrder(
            tenant_id=tenant,
            order_id=str(uuid.uuid4()),
            external_order_id=ext,
            source=source,
            source_status=source_status,
            canonical_status=canonical if validation != STATUS_REQUIRES_REVIEW else STATUS_REQUIRES_REVIEW,
            source_order_version=version,
            currency=currency,
            lines=tuple(lines),
            order_total=order_total,
            discount=_dec(raw.get("discount")) if "discount" in raw else None,
            payment_state=str(raw.get("payment_state") or PAY_UNKNOWN),
            fulfillment_state=str(raw.get("fulfillment_state") or FULFILL_UNKNOWN),
            customer_ref=str(raw.get("customer_ref") or f"cust:{ext}"),
            external_customer_ref=str(raw.get("external_customer_ref") or ""),
            provenance=str(raw.get("provenance") or "FIXTURE"),
            raw_hash=raw_hash,
            ingestion_mode=MODE_FIXTURE,
            idempotency_key=idem,
            validation_status=validation,
            ingest_result=INGEST_NEW,
            created_at=utc_now(),
            updated_at=utc_now(),
            economics_reference=eco,
            sale_price=_dec(raw.get("sale_price")) if "sale_price" in raw else None,
            list_price=_dec(raw.get("list_price")) if "list_price" in raw else None,
            marketplace_subsidy=_dec(raw.get("marketplace_subsidy")) if "marketplace_subsidy" in raw else None,
            contribution_estimate=eco.get("contribution_estimate") if isinstance(eco.get("contribution_estimate"), (str, type(None))) else None,
            cancelled=canonical == STATUS_CANCELLED,
            event_seq=ver_n or 1,
        )
        self.store.save(order)
        self.store.audit(tenant, {"event": "ORDER_RECEIVED", **order.public_audit_ref(), "source_status": source_status})
        self.store.audit(tenant, {"event": "ORDER_VALIDATED" if not ingest_issues else "ORDER_BLOCKED", **order.public_audit_ref(), "issue": ingest_issues})
        self.store.audit(tenant, {"event": "ORDER_CREATED", **order.public_audit_ref()})
        return order

    def get_order(self, order_id: str, *, tenant_id: str) -> CanonicalOrder:
        return self.store.get(order_id, tenant_id=tenant_id)

    def _plan_downstream(
        self,
        order: CanonicalOrder,
        *,
        tenant_id: str,
        requested_by: str,
        target: str,
        action: str,
        payload: dict,
        blocked_reason: str = "",
    ) -> PublicationPlan:
        tenant = require_tenant_id(tenant_id)
        if order.tenant_id != tenant:
            raise OrderOrchError(ORDER_ACCESS_DENIED)
        status = STATUS_BLOCKED if blocked_reason else STATUS_APPROVAL_REQUIRED
        key = idempotency_key(
            tenant_id=tenant,
            product_id=order.order_id,
            content_version=order.raw_hash,
            target=target,
            action=action,
            policy="order-orch-20",
        )
        done = self.store.completed(key)
        if done and done.execution_id:
            existing_plan = self.plans.receipt_for_key(tenant_id=tenant, key=key)
            # replay marker on store
        plan = PublicationPlan(
            plan_id=str(uuid.uuid4()),
            tenant_id=tenant,
            product_id=order.order_id,
            sku=order.external_order_id,
            article="",
            content_version=order.raw_hash,
            target=target,
            action=action,
            mode=MODE_FIXTURE,
            status=status,
            idempotency_key=key,
            snapshot_version=order.raw_hash,
            preview_id=str(uuid.uuid4()),
            payload=payload,
            warnings=(),
            issues=tuple([blocked_reason] if blocked_reason else ()),
            created_at=pub_utc(),
        )
        self.plans.save_plan(plan)
        self.store.audit(tenant, {"event": "DOWNSTREAM_PLANNED", "plan_id": plan.plan_id, "target": target, "action": action, "order_id": order.order_id})
        if status == STATUS_APPROVAL_REQUIRED:
            rec = self.governance.request(
                tenant_id=tenant,
                requested_by=requested_by,
                idempotency_key=key,
                content_version=order.raw_hash,
                snapshot_version=order.raw_hash,
                target=target,
                product_id=order.order_id,
                plan_id=plan.plan_id,
            )
            plan = replace(plan, approval_id=rec.approval_id)
            self.plans.save_plan(plan)
            self.store.audit(tenant, {"event": "APPROVAL_PENDING", "plan_id": plan.plan_id, "order_id": order.order_id})
        else:
            self.store.audit(tenant, {"event": "DOWNSTREAM_BLOCKED", "plan_id": plan.plan_id, "reason": blocked_reason, "order_id": order.order_id})
        return plan

    def plan_onec(self, order: CanonicalOrder, *, tenant_id: str, requested_by: str, action: str = "CREATE_ORDER") -> PublicationPlan:
        blocked = ""
        if action not in self.onec.SUPPORTED:
            blocked = "DOWNSTREAM_UNSUPPORTED"
        if any(ln.mapping_status != MAPPED for ln in order.lines) and action != "CANCEL_ORDER":
            blocked = MISSING_PRODUCT_MAPPING if any(ln.mapping_status == MISSING for ln in order.lines) else AMBIGUOUS_PRODUCT_MAPPING
        payload = {
            "order_id": order.order_id,
            "external_order_id": order.external_order_id,
            "source": order.source,
            "lines": [{"sku": ln.sku, "qty": str(ln.quantity), "product_id": ln.mapping_product_id} for ln in order.lines],
            "currency": order.currency,
            "order_total": str(order.order_total) if order.order_total is not None else None,
            "live": False,
        }
        return self._plan_downstream(order, tenant_id=tenant_id, requested_by=requested_by, target="ONEC", action=action, payload=payload, blocked_reason=blocked)

    def plan_crm(self, order: CanonicalOrder, *, tenant_id: str, requested_by: str, action: str = "LINK_CUSTOMER_REFERENCE") -> PublicationPlan:
        blocked = ""
        preview = self.crm.execute(action=action, payload={"order_id": order.order_id, "customer_ref": order.customer_ref})
        if preview.get("status") == "UNSUPPORTED":
            blocked = "DOWNSTREAM_UNSUPPORTED"
        payload = {
            "order_id": order.order_id,
            "external_order_id": order.external_order_id,
            "customer_ref": order.customer_ref,
            "source": order.source,
            "canonical_status": order.canonical_status,
            "currency": order.currency,
            "order_total": str(order.order_total) if order.order_total is not None else None,
            "live": False,
        }
        return self._plan_downstream(order, tenant_id=tenant_id, requested_by=requested_by, target="CRM", action=action, payload=payload, blocked_reason=blocked)

    def approve(self, plan_id: str, *, tenant_id: str, actor: str) -> PublicationPlan:
        plan = self.plans.get_plan(plan_id, tenant_id=tenant_id)
        self.governance.approve(plan.approval_id, tenant_id=tenant_id, actor=actor)
        plan = replace(plan, status=STATUS_APPROVED)
        self.plans.save_plan(plan)
        self.store.audit(tenant_id, {"event": "APPROVED", "plan_id": plan.plan_id, "order_id": plan.product_id})
        return plan

    def reject(self, plan_id: str, *, tenant_id: str, actor: str) -> PublicationPlan:
        plan = self.plans.get_plan(plan_id, tenant_id=tenant_id)
        self.governance.reject(plan.approval_id, tenant_id=tenant_id, actor=actor)
        plan = replace(plan, status=STATUS_REJECTED)
        self.plans.save_plan(plan)
        self.store.audit(tenant_id, {"event": "REJECTED", "plan_id": plan.plan_id, "order_id": plan.product_id})
        return plan

    def execute_downstream(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        actor: str,
        capabilities,
        order: CanonicalOrder | None = None,
        mode: str = MODE_FIXTURE,
    ) -> PublicationReceipt:
        tenant = require_tenant_id(tenant_id)
        self._mode(mode)
        plan = self.plans.get_plan(plan_id, tenant_id=tenant)
        needed = ONEC_WRITE_CAP if plan.target == "ONEC" else CRM_WRITE_CAP
        self._cap(capabilities, needed)
        if plan.status == STATUS_BLOCKED:
            raise OrderOrchError(ONEC_BLOCKED if plan.target == "ONEC" else "CRM_BLOCKED")
        if plan.status == STATUS_REJECTED:
            raise OrderOrchError(APPROVAL_REJECTED)
        done = self.store.completed(plan.idempotency_key)
        if done and done.execution_id:
            existing = self.plans.receipt_for_key(tenant_id=tenant, key=plan.idempotency_key)
            if existing:
                self.store.audit(tenant, {"event": "ORDER_REPLAYED", "receipt_id": existing.receipt_id, "order_id": plan.product_id})
                return replace(existing, status=STATUS_ALREADY_EXECUTED)
        approval = self.governance.get(plan.approval_id, tenant_id=tenant)
        content_ver = order.raw_hash if order is not None else plan.content_version
        try:
            self.governance.assert_valid_for_execute(
                approval, content_version=content_ver, snapshot_version=plan.snapshot_version, tenant_id=tenant
            )
        except OrderOrchError:
            self.plans.save_plan(replace(plan, status="STALE"))
            self.store.audit(tenant, {"event": "STALE", "plan_id": plan.plan_id, "order_id": plan.product_id})
            raise
        if plan.target == "ONEC":
            result = self.onec.execute(action=plan.action, payload=plan.payload)
            evt = "ONEC_EXECUTED_FIXTURE"
        else:
            result = self.crm.execute(action=plan.action, payload=plan.payload)
            evt = "CRM_EXECUTED_FIXTURE"
        if result.get("status") == "UNSUPPORTED":
            raise OrderOrchError("DOWNSTREAM_UNSUPPORTED")
        receipt = PublicationReceipt(
            receipt_id=str(uuid.uuid4()),
            tenant_id=tenant,
            target=plan.target,
            product_id=plan.product_id,
            content_version=plan.content_version,
            plan_id=plan.plan_id,
            idempotency_key=plan.idempotency_key,
            mode=MODE_FIXTURE,
            action=plan.action,
            status=STATUS_EXECUTED_FIXTURE,
            created_at=pub_utc(),
            approved_by=approval.approved_by or actor,
            audit_reference=f"audit:{tenant}:{plan.plan_id}",
            fixture_reference=result.get("fixture_reference") or "",
            published_live=False,
        )
        self.plans.save_receipt(receipt)
        self.store.remember(key=plan.idempotency_key, execution_id=receipt.receipt_id)
        self.plans.save_plan(replace(plan, status=STATUS_EXECUTED_FIXTURE))
        self.store.save_downstream(tenant_id=tenant, key=plan.idempotency_key, payload={"receipt_id": receipt.receipt_id, "target": plan.target})
        self.store.audit(tenant, {"event": evt, "receipt_id": receipt.receipt_id, "order_id": plan.product_id, "live": False})
        return receipt

    def aggregate(self, *, onec_status: str | None, crm_status: str | None) -> str:
        states = [s for s in (onec_status, crm_status) if s]
        if not states:
            return AGG_BLOCKED
        if all(s == STATUS_EXECUTED_FIXTURE for s in states):
            return AGG_COMPLETE
        if any(s == STATUS_EXECUTED_FIXTURE for s in states) and any(s != STATUS_EXECUTED_FIXTURE for s in states):
            return AGG_PARTIAL
        if any(s in {STATUS_BLOCKED, "UNSUPPORTED"} for s in states):
            return AGG_BLOCKED
        return AGG_REQUIRES_REVIEW
