"""Blocks 15–16 orchestrator: governed site publish + selective marketplace export. Offline only."""

from __future__ import annotations

import uuid
from dataclasses import replace

from commerce.capabilities import CAP_CATALOG_WRITE
from product_content.contracts import ProductContentPackage
from security.tenant import require_tenant_id

from governed_publish.adapters import FixtureMarketplaceAdapter, FixtureSiteAdapter
from governed_publish.contracts import (
    COMP_ROLLBACK_SUPPORTED,
    COMP_UNSUPPORTED,
    MODE_FIXTURE,
    MODE_LIVE,
    POLICY_VERSION,
    STATUS_ALREADY_EXECUTED,
    STATUS_APPROVAL_REQUIRED,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_EXECUTED_FIXTURE,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_REJECTED,
    TARGET_BITRIX_ASPRO,
    TARGET_OZON,
    TARGET_SITE,
    TARGET_WILDBERRIES,
    TARGET_YANDEX_MARKET,
    PublicationPlan,
    PublicationPreview,
    PublicationReceipt,
    idempotency_key,
    utc_now,
)
from governed_publish.diff import classify_diff, summarize
from governed_publish.eligibility import eligibility_for
from governed_publish.errors import (
    PUBLISH_ACCESS_DENIED,
    PUBLISH_BLOCKED,
    PUBLISH_LIVE_FORBIDDEN,
    PUBLISH_MODE_FORBIDDEN,
    GovernedPublishError,
)
from governed_publish.governance import PublicationGovernance, require_write_capability
from governed_publish.marketplace_payload import marketplace_payload, validate_marketplace_payload
from governed_publish.selection import select_packages
from governed_publish.site import canonical_site_fields, snapshot_version, to_bitrix_aspro_payload
from governed_publish.store import GovernedPublishStore


class GovernedPublishService:
    def __init__(
        self,
        store: GovernedPublishStore | None = None,
        governance: PublicationGovernance | None = None,
    ) -> None:
        self.store = store or GovernedPublishStore()
        self.governance = governance or PublicationGovernance()
        self.site_adapter = FixtureSiteAdapter()
        self.mp_adapter = FixtureMarketplaceAdapter()

    def _assert_mode(self, mode: str) -> str:
        mode = (mode or MODE_FIXTURE).upper()
        if mode == MODE_LIVE:
            raise GovernedPublishError(PUBLISH_LIVE_FORBIDDEN, "live_not_activated")
        if mode not in {MODE_FIXTURE, "SANDBOX"}:
            raise GovernedPublishError(PUBLISH_MODE_FORBIDDEN, mode)
        return MODE_FIXTURE  # sandbox still executes as fixture in this block

    def _economics_block(self, package: ProductContentPackage) -> bool:
        decision = str((package.card.economics_reference or {}).get("decision") or "")
        return decision in {"DENY"}

    def preview_site(
        self,
        package: ProductContentPackage,
        *,
        tenant_id: str,
        snapshot: dict | None = None,
        aspro: bool = True,
        mode: str = MODE_FIXTURE,
    ) -> PublicationPreview:
        tenant = require_tenant_id(tenant_id)
        if package.tenant_id != tenant:
            raise GovernedPublishError(PUBLISH_ACCESS_DENIED)
        self._assert_mode(mode)
        desired = to_bitrix_aspro_payload(package, aspro=aspro)
        desired_canonical = canonical_site_fields(package)
        entries = classify_diff(desired=desired, snapshot=snapshot)
        summary = summarize(entries)
        media_actions = tuple(f"include:{m['asset_id']}" for m in desired_canonical.get("media") or [])
        invalid = [a.asset_id for a in package.media.assets if a.validation_status != "VALID" and a.role != "THUMBNAIL"]
        seo_actions = ("seo_title", "meta_description", "slug")
        warnings = list(package.warnings)
        if invalid:
            warnings.append("invalid_media_excluded")
        preview = PublicationPreview(
            preview_id=str(uuid.uuid4()),
            tenant_id=tenant,
            target=TARGET_BITRIX_ASPRO if aspro else TARGET_SITE,
            product_id=package.product_id,
            content_version=package.version,
            snapshot_version=snapshot_version(snapshot),
            action=summary["action"],
            fields_create=summary["fields_create"],
            fields_change=summary["fields_change"],
            fields_unchanged=summary["fields_unchanged"],
            fields_omitted=summary["fields_omitted"],
            blocked_fields=summary["blocked_fields"],
            media_actions=media_actions,
            seo_actions=seo_actions,
            warnings=tuple(warnings),
            payload=desired,
            desired=desired,
            snapshot=dict(snapshot or {}),
        )
        self.store.audit(tenant, {"event": "preview_generated", "preview_id": preview.preview_id, "product_id": package.product_id, "version": package.version, "target": preview.target})
        return preview

    def plan_site(
        self,
        package: ProductContentPackage,
        *,
        tenant_id: str,
        requested_by: str,
        snapshot: dict | None = None,
        aspro: bool = True,
        mode: str = MODE_FIXTURE,
    ) -> PublicationPlan:
        tenant = require_tenant_id(tenant_id)
        self._assert_mode(mode)
        gate, reason = eligibility_for(package)
        preview = self.preview_site(package, tenant_id=tenant, snapshot=snapshot, aspro=aspro, mode=mode)
        issues: list[str] = []
        if gate == STATUS_BLOCKED:
            issues.append(reason)
        if self._economics_block(package):
            issues.append("economics_deny")
            gate = STATUS_BLOCKED
        key = idempotency_key(
            tenant_id=tenant,
            product_id=package.product_id,
            content_version=package.version,
            target=preview.target,
            action=preview.action,
            policy=POLICY_VERSION,
        )
        status = STATUS_BLOCKED if issues else STATUS_APPROVAL_REQUIRED
        plan = PublicationPlan(
            plan_id=str(uuid.uuid4()),
            tenant_id=tenant,
            product_id=package.product_id,
            sku=package.card.sku,
            article=package.card.article,
            content_version=package.version,
            target=preview.target,
            action=preview.action,
            mode=MODE_FIXTURE,
            status=status,
            idempotency_key=key,
            snapshot_version=preview.snapshot_version,
            preview_id=preview.preview_id,
            payload=preview.payload,
            warnings=preview.warnings,
            issues=tuple(issues),
            compensation=COMP_ROLLBACK_SUPPORTED,
            created_at=utc_now(),
        )
        self.store.save_plan(plan)
        self.store.audit(tenant, {"event": "plan_created", "plan_id": plan.plan_id, "status": plan.status, "product_id": plan.product_id, "version": plan.content_version})
        if status != STATUS_BLOCKED:
            rec = self.governance.request(
                tenant_id=tenant,
                requested_by=requested_by,
                idempotency_key=key,
                content_version=package.version,
                snapshot_version=preview.snapshot_version,
                target=preview.target,
                product_id=package.product_id,
                plan_id=plan.plan_id,
            )
            plan = replace(plan, approval_id=rec.approval_id, status=STATUS_APPROVAL_REQUIRED)
            self.store.save_plan(plan)
            self.store.audit(tenant, {"event": "approval_requested", "approval_id": rec.approval_id, "plan_id": plan.plan_id})
        return plan

    def approve(self, plan_id: str, *, tenant_id: str, actor: str) -> PublicationPlan:
        plan = self.store.get_plan(plan_id, tenant_id=tenant_id)
        rec = self.governance.approve(plan.approval_id, tenant_id=tenant_id, actor=actor)
        plan = replace(plan, status=STATUS_APPROVED)
        self.store.save_plan(plan)
        self.store.audit(tenant_id, {"event": "approval_granted", "approval_id": rec.approval_id, "plan_id": plan.plan_id, "actor": actor})
        return plan

    def reject(self, plan_id: str, *, tenant_id: str, actor: str) -> PublicationPlan:
        plan = self.store.get_plan(plan_id, tenant_id=tenant_id)
        self.governance.reject(plan.approval_id, tenant_id=tenant_id, actor=actor)
        plan = replace(plan, status=STATUS_REJECTED)
        self.store.save_plan(plan)
        self.store.audit(tenant_id, {"event": "approval_rejected", "plan_id": plan.plan_id, "actor": actor})
        return plan

    def execute(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        actor: str,
        capabilities: set[str] | None = None,
        package: ProductContentPackage | None = None,
        snapshot: dict | None = None,
        kind: str = "site",
    ) -> PublicationReceipt:
        tenant = require_tenant_id(tenant_id)
        require_write_capability(capabilities)
        plan = self.store.get_plan(plan_id, tenant_id=tenant)
        if plan.status == STATUS_BLOCKED:
            raise GovernedPublishError(PUBLISH_BLOCKED)
        done = self.store.completed(plan.idempotency_key)
        if done and done.execution_id:
            existing = self.store.receipt_for_key(tenant_id=tenant, key=plan.idempotency_key)
            if existing:
                self.store.audit(tenant, {"event": "idempotent_replay", "plan_id": plan.plan_id, "receipt_id": existing.receipt_id})
                return replace(existing, status=STATUS_ALREADY_EXECUTED)
        approval = self.governance.get(plan.approval_id, tenant_id=tenant)
        snap_ver = snapshot_version(snapshot) if snapshot is not None else plan.snapshot_version
        content_ver = package.version if package is not None else plan.content_version
        try:
            self.governance.assert_valid_for_execute(
                approval,
                content_version=content_ver,
                snapshot_version=snap_ver,
                tenant_id=tenant,
            )
        except GovernedPublishError:
            plan = replace(plan, status="STALE")
            self.store.save_plan(plan)
            self.store.audit(tenant, {"event": "stale_approval", "plan_id": plan.plan_id})
            raise
        if kind == "marketplace":
            result = self.mp_adapter.execute(tenant_id=tenant, target=plan.target, payload=plan.payload)
            compensation = COMP_UNSUPPORTED
        else:
            result = self.site_adapter.execute(tenant_id=tenant, payload=plan.payload)
            compensation = COMP_ROLLBACK_SUPPORTED
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
            compensation=compensation,
            published_live=False,
        )
        self.store.save_receipt(receipt)
        self.store.remember_execution(key=plan.idempotency_key, receipt_id=receipt.receipt_id)
        self.store.save_plan(replace(plan, status=STATUS_EXECUTED_FIXTURE))
        self.store.audit(tenant, {"event": "fixture_execution_result", "receipt_id": receipt.receipt_id, "plan_id": plan.plan_id, "status": receipt.status, "live": False})
        return receipt

    def plan_marketplace(
        self,
        package: ProductContentPackage,
        *,
        tenant_id: str,
        requested_by: str,
        target: str,
        snapshot: dict | None = None,
        category_map: dict | None = None,
        ambiguous: set[str] | None = None,
        mode: str = MODE_FIXTURE,
    ) -> PublicationPlan:
        tenant = require_tenant_id(tenant_id)
        if package.tenant_id != tenant:
            raise GovernedPublishError(PUBLISH_ACCESS_DENIED)
        self._assert_mode(mode)
        payload = marketplace_payload(package, target=target, category_map=category_map, ambiguous=ambiguous)
        issues = validate_marketplace_payload(payload, category=package.card.category)
        gate, reason = eligibility_for(package)
        if gate == STATUS_BLOCKED:
            issues.append(reason)
        if self._economics_block(package):
            issues.append("economics_deny")
        preview_entries = classify_diff(desired=payload, snapshot=snapshot)
        summary = summarize(preview_entries)
        key = idempotency_key(
            tenant_id=tenant,
            product_id=package.product_id,
            content_version=package.version,
            target=target,
            action=summary["action"],
            policy=POLICY_VERSION,
        )
        status = STATUS_BLOCKED if issues else STATUS_APPROVAL_REQUIRED
        plan = PublicationPlan(
            plan_id=str(uuid.uuid4()),
            tenant_id=tenant,
            product_id=package.product_id,
            sku=package.card.sku,
            article=package.card.article,
            content_version=package.version,
            target=target,
            action=summary["action"],
            mode=MODE_FIXTURE,
            status=status,
            idempotency_key=key,
            snapshot_version=snapshot_version(snapshot),
            preview_id=str(uuid.uuid4()),
            payload=payload,
            warnings=tuple(package.warnings),
            issues=tuple(dict.fromkeys(issues)),
            compensation=COMP_UNSUPPORTED,
            created_at=utc_now(),
        )
        self.store.save_plan(plan)
        self.store.audit(tenant, {"event": "plan_created", "plan_id": plan.plan_id, "target": target, "status": status, "product_id": package.product_id, "version": package.version})
        if status != STATUS_BLOCKED:
            rec = self.governance.request(
                tenant_id=tenant,
                requested_by=requested_by,
                idempotency_key=key,
                content_version=package.version,
                snapshot_version=plan.snapshot_version,
                target=target,
                product_id=package.product_id,
                plan_id=plan.plan_id,
            )
            plan = replace(plan, approval_id=rec.approval_id)
            self.store.save_plan(plan)
            self.store.audit(tenant, {"event": "approval_requested", "approval_id": rec.approval_id, "plan_id": plan.plan_id})
        return plan

    def export_batch(
        self,
        packages: list[ProductContentPackage],
        *,
        tenant_id: str,
        requested_by: str,
        actor: str,
        capabilities: set[str],
        target: str,
        product_ids: tuple[str, ...] = (),
        skus: tuple[str, ...] = (),
        articles: tuple[str, ...] = (),
        categories: tuple[str, ...] = (),
        exclude_ids: tuple[str, ...] = (),
        category_map: dict | None = None,
        execute: bool = False,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        selection = select_packages(
            packages,
            tenant_id=tenant,
            product_ids=product_ids,
            skus=skus,
            articles=articles,
            categories=categories,
            exclude_ids=exclude_ids,
        )
        by_id = {p.product_id: p for p in packages}
        results: list[dict] = []
        for pid in selection.selected:
            pkg = by_id[pid]
            plan = self.plan_marketplace(
                pkg,
                tenant_id=tenant,
                requested_by=requested_by,
                target=target,
                category_map=category_map,
            )
            item = {"product_id": pid, "plan_id": plan.plan_id, "status": plan.status, "issues": list(plan.issues)}
            if execute and plan.status != STATUS_BLOCKED:
                self.approve(plan.plan_id, tenant_id=tenant, actor=actor)
                try:
                    rec = self.execute(
                        plan.plan_id,
                        tenant_id=tenant,
                        actor=actor,
                        capabilities=capabilities,
                        package=pkg,
                        kind="marketplace",
                    )
                    item["status"] = rec.status
                    item["receipt_id"] = rec.receipt_id
                    item["fixture_reference"] = rec.fixture_reference
                except GovernedPublishError as exc:
                    item["status"] = STATUS_FAILED
                    item["error"] = exc.code
            results.append(item)
        return {
            "selection": selection.inspectable,
            "results": results,
            "partial": any(r["status"] != STATUS_EXECUTED_FIXTURE for r in results) and any(r.get("receipt_id") for r in results),
            "mode": MODE_FIXTURE,
            "published_live": False,
        }
