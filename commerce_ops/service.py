"""Blocks 17–19 orchestrator: price update, margin protection, stock sync. FIXTURE only."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from decimal import Decimal

from commerce.capabilities import CAP_PRICING_WRITE, CAP_STOCK_WRITE
from data_intel.economics import EconomicsInput, EconomicsPolicy, PROV_USER
from governed_publish.contracts import (
    MODE_FIXTURE,
    MODE_LIVE,
    POLICY_VERSION,
    STATUS_ALREADY_EXECUTED,
    STATUS_APPROVAL_REQUIRED,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_EXECUTED_FIXTURE,
    STATUS_REJECTED,
    PublicationPlan,
    PublicationReceipt,
    idempotency_key,
    utc_now,
)
from governed_publish.governance import PublicationGovernance
from governed_publish.marketplace_payload import map_marketplace_category
from governed_publish.selection import select_packages
from governed_publish.store import GovernedPublishStore
from product_content.contracts import ProductContentPackage
from security.tenant import require_tenant_id

from commerce_ops.adapters import FixturePriceAdapter, FixtureStockAdapter
from commerce_ops.errors import (
    ACCESS_DENIED,
    ALREADY_EXECUTED,
    APPROVAL_REJECTED,
    CAPABILITY_DENIED,
    EMPTY_SELECTION,
    LIVE_FORBIDDEN,
    MISSING_MAPPING,
    AMBIGUOUS_MAPPING,
    NOOP_PRICE,
    OVERRIDE_FORBIDDEN,
    STALE_APPROVAL,
    STALE_ECONOMICS,
    UNSAFE_PRICE,
    CommerceOpsError,
)
from commerce_ops.protection import evaluate_proposed_price
from commerce_ops.stock_calc import published_quantity


def _econ_version(inp: EconomicsInput, proposed: str) -> str:
    blob = json.dumps(
        {
            "purchase": str(inp.purchase_price),
            "channel": inp.channel,
            "commission": str(inp.commission_rate),
            "logistics": str(inp.logistics_cost),
            "advertising": str(inp.advertising_cost),
            "ownership": inp.discount_ownership,
            "proposed": proposed,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _price_action(current, proposed) -> str:
    if current is None or proposed is None:
        return "UNKNOWN"
    c, p = Decimal(str(current)), Decimal(str(proposed))
    if p == c:
        return "UNCHANGED"
    if p > c:
        return "INCREASE"
    return "DECREASE"


class CommerceOpsService:
    def __init__(
        self,
        store: GovernedPublishStore | None = None,
        governance: PublicationGovernance | None = None,
    ) -> None:
        self.store = store or GovernedPublishStore()
        self.governance = governance or PublicationGovernance()
        self.price_adapter = FixturePriceAdapter()
        self.stock_adapter = FixtureStockAdapter()

    def _mode(self, mode: str) -> str:
        mode = (mode or MODE_FIXTURE).upper()
        if mode == MODE_LIVE:
            raise CommerceOpsError(LIVE_FORBIDDEN)
        return MODE_FIXTURE

    def _cap(self, capabilities, needed: str) -> None:
        if needed not in set(capabilities or ()):
            raise CommerceOpsError(CAPABILITY_DENIED)

    def plan_price(
        self,
        *,
        tenant_id: str,
        package: ProductContentPackage,
        target: str,
        proposed_price: Decimal | None,
        current_price: Decimal | None,
        economics: EconomicsInput,
        requested_by: str,
        reason: str = "",
        provenance: str = PROV_USER,
        snapshot: dict | None = None,
        category_map: dict | None = None,
        ambiguous: set[str] | None = None,
        override: bool = False,
        override_reason: str = "",
        policy: EconomicsPolicy | None = None,
        require_costs: tuple[str, ...] = (),
        mode: str = MODE_FIXTURE,
    ) -> PublicationPlan:
        tenant = require_tenant_id(tenant_id)
        if package.tenant_id != tenant:
            raise CommerceOpsError(ACCESS_DENIED)
        self._mode(mode)
        issues: list[str] = []
        if target not in {"SITE", "CUSTOM"}:
            cat_status, _ = map_marketplace_category(
                target, package.card.category, category_map=category_map, ambiguous=ambiguous
            )
            if cat_status == "MISSING":
                issues.append(MISSING_MAPPING)
            if cat_status == "AMBIGUOUS":
                issues.append(AMBIGUOUS_MAPPING)
        action = _price_action(current_price, proposed_price)
        safety = evaluate_proposed_price(
            economics, proposed=proposed_price, policy=policy, require_costs=require_costs
        )
        econ_ver = _econ_version(economics, str(proposed_price))
        snap_ver = (snapshot or {}).get("version") or f"price:{current_price}"
        bind = f"{snap_ver}|{econ_ver}"
        if action == "UNCHANGED":
            status = "UNCHANGED"
            issues.append(NOOP_PRICE)
        elif issues:
            status = STATUS_BLOCKED
        elif safety["decision"] in {"DENY"} and not override:
            status = STATUS_BLOCKED
            issues.append(safety.get("code") or UNSAFE_PRICE)
        elif safety["decision"] == "REQUIRE_REVIEW" and not override:
            status = STATUS_BLOCKED
            issues.append(safety.get("code") or "INCOMPLETE_ECONOMICS")
        elif override and safety["decision"] == "DENY":
            if not override_reason:
                raise CommerceOpsError(OVERRIDE_FORBIDDEN, "override_reason_required")
            status = STATUS_APPROVAL_REQUIRED
            issues.append("override_requested")
        else:
            status = STATUS_APPROVAL_REQUIRED
            if safety["decision"] == "DENY":
                issues.append(safety.get("code") or UNSAFE_PRICE)
        payload = {
            "kind": "price",
            "current_price": str(current_price) if current_price is not None else None,
            "proposed_price": str(proposed_price) if proposed_price is not None else None,
            "currency": economics.currency,
            "provenance": provenance,
            "economics_decision": safety["decision"],
            "minimum_price": safety.get("minimum_price"),
            "economics_version": econ_ver,
            "override": override,
            "override_reason": override_reason,
            "reason": reason,
            "action": action,
            "contribution_note": safety.get("contribution_note"),
            "discount_note": safety.get("discount_note"),
            "marketplace_subsidy": safety.get("marketplace_subsidy"),
            "effective_price": safety.get("effective_price"),
        }
        key = idempotency_key(
            tenant_id=tenant,
            product_id=package.product_id,
            content_version=package.version,
            target=target,
            action=f"price:{action}:{proposed_price}:{econ_ver}",
            policy=POLICY_VERSION,
        )
        plan = PublicationPlan(
            plan_id=str(uuid.uuid4()),
            tenant_id=tenant,
            product_id=package.product_id,
            sku=package.card.sku,
            article=package.card.article,
            content_version=package.version,
            target=target,
            action=action,
            mode=MODE_FIXTURE,
            status=status,
            idempotency_key=key,
            snapshot_version=bind,
            preview_id=str(uuid.uuid4()),
            payload=payload,
            warnings=tuple(safety.get("missing") or ()),
            issues=tuple(dict.fromkeys(issues)),
            created_at=utc_now(),
        )
        self.store.save_plan(plan)
        self.store.audit(tenant, {"event": "PLANNED", "plan_id": plan.plan_id, "kind": "price", "target": target, "decision": safety["decision"]})
        if status == STATUS_APPROVAL_REQUIRED:
            rec = self.governance.request(
                tenant_id=tenant,
                requested_by=requested_by,
                idempotency_key=key,
                content_version=package.version,
                snapshot_version=bind,
                target=target,
                product_id=package.product_id,
                plan_id=plan.plan_id,
            )
            plan = replace(plan, approval_id=rec.approval_id)
            self.store.save_plan(plan)
            self.store.audit(tenant, {"event": "APPROVAL_PENDING", "approval_id": rec.approval_id, "plan_id": plan.plan_id})
        elif status == STATUS_BLOCKED:
            self.store.audit(tenant, {"event": "BLOCKED", "plan_id": plan.plan_id, "issues": list(plan.issues)})
        return plan

    def approve(self, plan_id: str, *, tenant_id: str, actor: str) -> PublicationPlan:
        plan = self.store.get_plan(plan_id, tenant_id=tenant_id)
        self.governance.approve(plan.approval_id, tenant_id=tenant_id, actor=actor)
        plan = replace(plan, status=STATUS_APPROVED)
        self.store.save_plan(plan)
        self.store.audit(tenant_id, {"event": "APPROVED", "plan_id": plan.plan_id, "actor": actor})
        return plan

    def reject(self, plan_id: str, *, tenant_id: str, actor: str) -> PublicationPlan:
        plan = self.store.get_plan(plan_id, tenant_id=tenant_id)
        self.governance.reject(plan.approval_id, tenant_id=tenant_id, actor=actor)
        plan = replace(plan, status=STATUS_REJECTED)
        self.store.save_plan(plan)
        self.store.audit(tenant_id, {"event": "REJECTED", "plan_id": plan.plan_id})
        return plan

    def execute(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        actor: str,
        capabilities,
        package: ProductContentPackage | None = None,
        economics: EconomicsInput | None = None,
        proposed_price=None,
        snapshot: dict | None = None,
        kind: str = "price",
    ) -> PublicationReceipt:
        tenant = require_tenant_id(tenant_id)
        plan = self.store.get_plan(plan_id, tenant_id=tenant)
        needed = CAP_PRICING_WRITE if plan.payload.get("kind") == "price" else CAP_STOCK_WRITE
        self._cap(capabilities, needed)
        if plan.status == STATUS_BLOCKED:
            raise CommerceOpsError(plan.issues[0] if plan.issues else UNSAFE_PRICE)
        if plan.status == "UNCHANGED" or plan.payload.get("action") == "UNCHANGED":
            raise CommerceOpsError(NOOP_PRICE)
        if plan.status == STATUS_REJECTED:
            raise CommerceOpsError(APPROVAL_REJECTED)
        done = self.store.completed(plan.idempotency_key)
        if done and done.execution_id:
            existing = self.store.receipt_for_key(tenant_id=tenant, key=plan.idempotency_key)
            if existing:
                self.store.audit(tenant, {"event": "REPLAYED", "receipt_id": existing.receipt_id})
                return replace(existing, status=STATUS_ALREADY_EXECUTED)
        if plan.payload.get("override") and plan.payload.get("economics_decision") == "DENY":
            if not plan.payload.get("override_reason"):
                raise CommerceOpsError(OVERRIDE_FORBIDDEN)
        approval = self.governance.get(plan.approval_id, tenant_id=tenant)
        content_ver = package.version if package is not None else plan.content_version
        try:
            if economics is not None and proposed_price is not None:
                if _econ_version(economics, str(proposed_price)) != plan.payload.get("economics_version"):
                    raise CommerceOpsError(STALE_ECONOMICS)
            if snapshot is not None and str(snapshot.get("version") or "") not in {"", plan.snapshot_version}:
                raise CommerceOpsError(STALE_APPROVAL, "snapshot_mismatch")
            self.governance.assert_valid_for_execute(
                approval,
                content_version=content_ver,
                snapshot_version=plan.snapshot_version,
                tenant_id=tenant,
            )
        except CommerceOpsError:
            self.store.save_plan(replace(plan, status="STALE"))
            self.store.audit(tenant, {"event": "STALE", "plan_id": plan.plan_id})
            raise
        if plan.payload.get("kind") == "stock":
            result = self.stock_adapter.execute(tenant_id=tenant, target=plan.target, payload=plan.payload)
        else:
            result = self.price_adapter.execute(tenant_id=tenant, target=plan.target, payload=plan.payload)
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
            created_at=utc_now(),
            approved_by=approval.approved_by or actor,
            audit_reference=f"audit:{tenant}:{plan.plan_id}",
            fixture_reference=result["fixture_reference"],
            warnings=plan.warnings,
            published_live=False,
        )
        self.store.save_receipt(receipt)
        self.store.remember_execution(key=plan.idempotency_key, receipt_id=receipt.receipt_id)
        self.store.save_plan(replace(plan, status=STATUS_EXECUTED_FIXTURE))
        self.store.audit(tenant, {"event": "EXECUTED_FIXTURE", "receipt_id": receipt.receipt_id, "live": False})
        return receipt

    def plan_stock(
        self,
        *,
        tenant_id: str,
        package: ProductContentPackage,
        target: str,
        available,
        requested_by: str,
        reserved=None,
        safety_stock=None,
        freshness: str = "FRESH",
        provenance: str = "FILE_PROVIDED",
        category_map: dict | None = None,
        ambiguous: set[str] | None = None,
        mode: str = MODE_FIXTURE,
    ) -> PublicationPlan:
        tenant = require_tenant_id(tenant_id)
        if package.tenant_id != tenant:
            raise CommerceOpsError(ACCESS_DENIED)
        self._mode(mode)
        issues: list[str] = []
        if target not in {"SITE", "CUSTOM"}:
            cat_status, _ = map_marketplace_category(target, package.card.category, category_map=category_map, ambiguous=ambiguous)
            if cat_status == "MISSING":
                issues.append(MISSING_MAPPING)
            if cat_status == "AMBIGUOUS":
                issues.append(AMBIGUOUS_MAPPING)
        try:
            calc = published_quantity(available=available, reserved=reserved, safety_stock=safety_stock, freshness=freshness)
        except ValueError:
            calc = {"decision": "DENY", "code": "INVALID_STOCK", "published": None, "kind": "UNKNOWN", "issues": ("negative",)}
        if calc["decision"] in {"DENY", "REQUIRE_REVIEW"} or issues:
            status = STATUS_BLOCKED
            issues.append(calc.get("code") or "INVALID_STOCK")
        else:
            status = STATUS_APPROVAL_REQUIRED
        payload = {
            "kind": "stock",
            "available": None if calc.get("kind") == "UNKNOWN" else calc.get("available"),
            "kind_qty": calc.get("kind"),
            "safety_stock": calc.get("safety_stock"),
            "published": calc.get("published"),
            "freshness": freshness,
            "provenance": provenance,
            "reserved": None if reserved in (None, "") else str(reserved),
        }
        bind = f"stock:{freshness}:{available}:{calc.get('published')}"
        key = idempotency_key(
            tenant_id=tenant,
            product_id=package.product_id,
            content_version=package.version,
            target=target,
            action=f"stock:{calc.get('published')}:{freshness}",
            policy=POLICY_VERSION,
        )
        plan = PublicationPlan(
            plan_id=str(uuid.uuid4()),
            tenant_id=tenant,
            product_id=package.product_id,
            sku=package.card.sku,
            article=package.card.article,
            content_version=package.version,
            target=target,
            action="STOCK_SYNC",
            mode=MODE_FIXTURE,
            status=status,
            idempotency_key=key,
            snapshot_version=bind,
            preview_id=str(uuid.uuid4()),
            payload=payload,
            warnings=tuple(calc.get("issues") or ()),
            issues=tuple(dict.fromkeys(issues)),
            created_at=utc_now(),
        )
        self.store.save_plan(plan)
        self.store.audit(tenant, {"event": "PLANNED", "plan_id": plan.plan_id, "kind": "stock", "target": target})
        if status == STATUS_APPROVAL_REQUIRED:
            rec = self.governance.request(
                tenant_id=tenant,
                requested_by=requested_by,
                idempotency_key=key,
                content_version=package.version,
                snapshot_version=bind,
                target=target,
                product_id=package.product_id,
                plan_id=plan.plan_id,
            )
            plan = replace(plan, approval_id=rec.approval_id)
            self.store.save_plan(plan)
            self.store.audit(tenant, {"event": "APPROVAL_PENDING", "plan_id": plan.plan_id})
        else:
            self.store.audit(tenant, {"event": "BLOCKED", "plan_id": plan.plan_id, "issues": list(plan.issues)})
        return plan

    def sync_stock_batch(
        self,
        packages: list[ProductContentPackage],
        *,
        tenant_id: str,
        target: str,
        quantities: dict[str, object],
        requested_by: str,
        actor: str,
        capabilities,
        product_ids: tuple[str, ...] = (),
        skus: tuple[str, ...] = (),
        articles: tuple[str, ...] = (),
        categories: tuple[str, ...] = (),
        exclude_ids: tuple[str, ...] = (),
        freshness: str = "FRESH",
        safety_stock=None,
        execute: bool = False,
        category_map: dict | None = None,
    ) -> dict:
        selection = select_packages(
            packages,
            tenant_id=tenant_id,
            product_ids=product_ids,
            skus=skus,
            articles=articles,
            categories=categories,
            exclude_ids=exclude_ids,
        )
        by_id = {p.product_id: p for p in packages}
        results = []
        for pid in selection.selected:
            pkg = by_id[pid]
            plan = self.plan_stock(
                tenant_id=tenant_id,
                package=pkg,
                target=target,
                available=quantities.get(pid, "UNKNOWN"),
                requested_by=requested_by,
                safety_stock=safety_stock,
                freshness=freshness,
                category_map=category_map,
            )
            item = {"product_id": pid, "plan_id": plan.plan_id, "status": plan.status, "issues": list(plan.issues), "target": target}
            if execute and plan.status == STATUS_APPROVAL_REQUIRED:
                self.approve(plan.plan_id, tenant_id=tenant_id, actor=actor)
                rec = self.execute(plan.plan_id, tenant_id=tenant_id, actor=actor, capabilities=capabilities, package=pkg, kind="stock")
                item["status"] = rec.status
                item["receipt_id"] = rec.receipt_id
            results.append(item)
        return {"selection": selection.inspectable, "results": results, "published_live": False, "mode": MODE_FIXTURE}
