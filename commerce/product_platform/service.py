"""Product Platform Service — Block 11 facade."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from commerce.product_platform.catalog import analyze_catalog
from commerce.product_platform.cms import FakeCommerceCmsProvider
from commerce.product_platform.errors import (
    COMMERCE_ACCESS_DENIED,
    COMMERCE_APPROVAL_REPLAY,
    COMMERCE_CROSS_TENANT,
    COMMERCE_ENRICHMENT_CONFLICT,
    COMMERCE_NOT_FOUND,
    COMMERCE_ORDER_INVALID,
    COMMERCE_ORDER_TRANSITION_INVALID,
    COMMERCE_PRICE_DENIED,
    COMMERCE_PRICE_STALE_DECISION,
    COMMERCE_STOCK_STALE,
    ProductPlatformError,
)
from commerce.product_platform.models import (
    IMPORT_PROFILE_VERSION,
    MATCH_AMBIGUOUS,
    MATCH_CONFLICT,
    MATCH_MATCHED,
    MATCH_NEW,
    MATCH_UNCHANGED,
    ORDER_CANCELLED,
    ORDER_CONFIRMED,
    ORDER_FAILED,
    ORDER_FULFILLED,
    ORDER_NEW,
    ORDER_PROCESSING,
    ORDER_TRANSITIONS,
    PRICE_ALLOW,
    PRICE_DENY,
    PRICE_REQUIRE_APPROVAL,
    TRUST_GENERATED,
    TRUST_INFERRED,
    TRUST_TRUSTED,
    CmsOperationResult,
    CommerceJob,
    MoneyAmount,
    PlatformOrder,
    PlatformOrderItem,
    PriceChangeReceipt,
    PriceDecision,
    PricePolicy,
    ProductImportResult,
    ProductVersion,
    observation_hash,
)
from commerce.product_platform.observability import CommerceObservability
from commerce.product_platform.planner import assert_sync_commerce_allowed, classify_import_workload
from commerce.product_platform.policy import BULK_APPLY_BATCH_SIZE, BULK_CMS_SYNC_BATCH_SIZE, PRICE_POLICY_VERSION
from commerce.product_platform.pricing import evaluate_price_decision, is_outlier, observation_is_fresh
from commerce.product_platform.repository import ProductPlatformRepository
from commerce.store import CommerceStore
from security.tenant import require_tenant_id

_ORDER_TRANSITIONS = ORDER_TRANSITIONS


def _version_payload(version: ProductVersion) -> dict:
    return {
        "product_id": version.product_id,
        "version_id": version.version_id,
        "tenant_id": version.tenant_id,
        "title": version.title,
        "brand": version.brand,
        "description": version.description,
        "sku": version.sku,
        "status": version.status,
        "parent_version_id": version.parent_version_id,
        "field_trust": dict(version.field_trust),
        "attributes": dict(version.attributes),
        "category_id": version.category_id,
        "publication_readiness": version.publication_readiness,
        "profile_version": version.profile_version,
        "created_at": version.created_at.isoformat(),
    }


class ProductPlatformService:
    def __init__(
        self,
        *,
        store: CommerceStore | None = None,
        repository: ProductPlatformRepository | None = None,
        cms: FakeCommerceCmsProvider | None = None,
        obs: CommerceObservability | None = None,
        product_media_service=None,
        content_intelligence_service=None,
    ):
        self.store = store or CommerceStore(path=":memory:")
        self.repo = repository or ProductPlatformRepository(self.store)
        self.cms = cms or FakeCommerceCmsProvider()
        self.obs = obs or CommerceObservability()
        self.product_media = product_media_service
        self.content_intel = content_intelligence_service
        self._approvals: dict[str, dict] = {}

    def create_product_version(
        self,
        *,
        tenant_id: str,
        title: str,
        sku: str = "",
        brand: str = "",
        parent_version_id: str | None = None,
        product_id: str | None = None,
        field_trust: dict | None = None,
        payload_tenant: str | None = None,
    ) -> ProductVersion:
        tenant = require_tenant_id(tenant_id)
        if payload_tenant and require_tenant_id(payload_tenant) != tenant:
            raise ProductPlatformError(COMMERCE_CROSS_TENANT)
        pid = product_id or str(uuid.uuid4())
        vid = str(uuid.uuid4())
        version = ProductVersion(
            product_id=pid,
            version_id=vid,
            tenant_id=tenant,
            title=title,
            sku=sku,
            brand=brand,
            parent_version_id=parent_version_id,
            field_trust=field_trust or {"title": TRUST_TRUSTED, "sku": TRUST_TRUSTED},
        )
        if sku:
            existing = self.repo.find_by_identifier(tenant, "sku", sku)
            if existing and existing != pid:
                raise ProductPlatformError("COMMERCE_IDENTIFIER_CONFLICT")
            self.repo.bind_identifier(tenant, "sku", sku, pid)
        self.repo.save_product_version(tenant, pid, vid, _version_payload(version))
        self.obs.emit("commerce.product.created", metadata={"product_id": pid, "version_id": vid})
        self.obs.emit("commerce.product.versioned", metadata={"product_id": pid, "version_id": vid})
        return version

    def get_product(self, *, tenant_id: str, product_id: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        return self.repo.get_product(tenant, product_id)

    def import_preview(
        self,
        *,
        tenant_id: str,
        rows: list[dict],
        mapping: dict[str, str] | None = None,
    ) -> ProductImportResult:
        tenant = require_tenant_id(tenant_id)
        mapping = mapping or {"sku": "sku", "title": "title", "brand": "brand"}
        created = updated = unchanged = conflicts = invalid = ambiguous = 0
        details: list[dict] = []
        for idx, row in enumerate(rows):
            sku = str(row.get(mapping.get("sku", "sku")) or "").strip()
            title = str(row.get(mapping.get("title", "title")) or "").strip()
            if not sku or not title:
                invalid += 1
                details.append({"row": idx, "state": "INVALID"})
                continue
            existing = self.repo.find_by_identifier(tenant, "sku", sku)
            if existing:
                current = self.repo.get_product(tenant, existing)
                if current and current.get("title") == title:
                    unchanged += 1
                    details.append({"row": idx, "state": MATCH_UNCHANGED, "product_id": existing})
                else:
                    updated += 1
                    details.append({"row": idx, "state": MATCH_MATCHED, "product_id": existing})
            else:
                dup_title = [p for p in self.repo.list_products(tenant) if p.get("title") == title]
                if len(dup_title) > 1:
                    ambiguous += 1
                    details.append({"row": idx, "state": MATCH_AMBIGUOUS})
                else:
                    created += 1
                    details.append({"row": idx, "state": MATCH_NEW, "sku": sku})
        return ProductImportResult(
            import_id=str(uuid.uuid4()),
            tenant_id=tenant,
            dry_run=True,
            created=created,
            updated=updated,
            unchanged=unchanged,
            conflicts=conflicts,
            invalid=invalid,
            ambiguous=ambiguous,
            details=tuple(details),
        )

    def import_products(
        self,
        *,
        tenant_id: str,
        rows: list[dict],
        dry_run: bool = False,
        bulk: bool = False,
        resume_from: int = 0,
        job_id: str | None = None,
    ) -> ProductImportResult:
        tenant = require_tenant_id(tenant_id)
        workload = classify_import_workload(len(rows))
        if workload != "sync" and not bulk:
            assert_sync_commerce_allowed(row_count=len(rows))
        preview = self.import_preview(tenant_id=tenant, rows=rows)
        if dry_run:
            return preview
        self.obs.emit("commerce.import.started", metadata={"rows": len(rows), "dry_run": False})
        applied = 0
        jid = job_id or str(uuid.uuid4())
        for idx in range(resume_from, len(rows)):
            row = rows[idx]
            sku = str(row.get("sku") or "").strip()
            title = str(row.get("title") or "").strip()
            if not sku or not title:
                continue
            existing = self.repo.find_by_identifier(tenant, "sku", sku)
            if existing:
                current = self.repo.get_product(tenant, existing)
                if current and current.get("title") != title:
                    parent = current["version_id"]
                    self.create_product_version(
                        tenant_id=tenant,
                        title=title,
                        sku=sku,
                        brand=str(row.get("brand") or ""),
                        product_id=existing,
                        parent_version_id=parent,
                    )
            else:
                self.create_product_version(
                    tenant_id=tenant,
                    title=title,
                    sku=sku,
                    brand=str(row.get("brand") or ""),
                )
            applied += 1
            if applied >= 100:
                break
        checkpoint = resume_from + applied
        status = "completed" if checkpoint >= len(rows) else "partial"
        self.repo.save_import_job(
            tenant,
            jid,
            {"checkpoint": checkpoint, "total": len(rows), "status": status, "profile": IMPORT_PROFILE_VERSION},
        )
        self.obs.emit("commerce.import.completed", metadata={"job_id": jid, "checkpoint": checkpoint})
        return ProductImportResult(
            import_id=jid,
            tenant_id=tenant,
            dry_run=False,
            created=preview.created,
            updated=preview.updated,
            unchanged=preview.unchanged,
            conflicts=preview.conflicts,
            invalid=preview.invalid,
            ambiguous=preview.ambiguous,
        )

    def enrich_product(
        self,
        *,
        tenant_id: str,
        product_id: str,
        generated_description: str,
        generated_price: Decimal | None = None,
        overwrite_trusted: bool = False,
    ) -> ProductVersion:
        tenant = require_tenant_id(tenant_id)
        current = self.repo.get_product(tenant, product_id)
        if current is None:
            raise ProductPlatformError(COMMERCE_NOT_FOUND)
        trust = dict(current.get("field_trust") or {})
        if generated_price is not None and trust.get("price") == TRUST_TRUSTED and not overwrite_trusted:
            raise ProductPlatformError(COMMERCE_ENRICHMENT_CONFLICT)
        field_trust = dict(trust)
        field_trust["description"] = TRUST_GENERATED
        if generated_price is not None and overwrite_trusted:
            field_trust["price"] = TRUST_GENERATED
        version = self.create_product_version(
            tenant_id=tenant,
            title=current["title"],
            sku=current.get("sku", ""),
            brand=current.get("brand", ""),
            product_id=product_id,
            parent_version_id=current["version_id"],
            field_trust=field_trust,
        )
        payload = _version_payload(version)
        payload["description"] = generated_description
        self.repo.save_product_version(tenant, product_id, version.version_id, payload)
        self.obs.emit("commerce.enrichment.completed", metadata={"product_id": product_id})
        return version

    def analyze_catalog(self, *, tenant_id: str, profile: str = "marketplace") -> dict:
        tenant = require_tenant_id(tenant_id)
        products = self.repo.list_products(tenant)
        prices = {}
        stock = {}
        media = {}
        for p in products:
            pid = p["product_id"]
            prices[pid] = self.repo.get_price(tenant, pid, "RUB") is not None
            inv = self.repo.get_inventory(tenant, pid, "main")
            stock[pid] = inv is not None and inv["on_hand"] > 0
            media[pid] = bool(p.get("primary_media_ref"))
        report = analyze_catalog(
            tenant_id=tenant,
            products=products,
            prices=prices,
            stock=stock,
            media=media,
            profile=profile,
        )
        self.obs.emit("commerce.catalog.analyzed", metadata={"issues": len(report.issues)})
        return {
            "report_id": report.report_id,
            "issues": [asdict(i) for i in report.issues],
            "counts": dict(report.counts),
        }

    def observe_price(
        self,
        *,
        tenant_id: str,
        product_id: str,
        source: str,
        amount: Decimal,
        currency: str,
        observed_at: datetime | None = None,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        observed_at = observed_at or datetime.now(timezone.utc)
        money = MoneyAmount(amount, currency)
        chash = observation_hash(
            product_id=product_id, source=source, price=amount, currency=currency, observed_at=observed_at
        )
        obs_id = str(uuid.uuid4())
        payload = {
            "observation_id": obs_id,
            "tenant_id": tenant,
            "product_id": product_id,
            "source": source,
            "price": str(amount),
            "currency": currency,
            "observed_at": observed_at.isoformat(),
        }
        inserted = self.repo.save_observation(tenant, obs_id, payload, chash)
        self.obs.emit("commerce.price.observed", metadata={"product_id": product_id, "source": source})
        return {"observation_id": obs_id, "deduped": not inserted}

    def decide_price(
        self,
        *,
        tenant_id: str,
        product_id: str,
        proposed_amount: Decimal,
        currency: str = "RUB",
        policy: PricePolicy | None = None,
    ) -> PriceDecision:
        tenant = require_tenant_id(tenant_id)
        policy = policy or PricePolicy(
            policy_id="default",
            tenant_id=tenant,
            version=PRICE_POLICY_VERSION,
            currency=currency,
            minimum_price=Decimal("1.00"),
            maximum_price=Decimal("1000000.00"),
            minimum_margin_pct=Decimal("10"),
            max_change_pct=Decimal("20"),
            max_change_abs=Decimal("5000"),
            auto_apply_max_change_pct=Decimal("5"),
        )
        current_row = self.repo.get_price(tenant, product_id, currency)
        if current_row is None:
            current = MoneyAmount(Decimal("0"), currency)
            price_version = 0
        else:
            current = MoneyAmount(current_row[0], currency)
            price_version = current_row[1]
        cost_amt = self.repo.get_trusted_cost(tenant, product_id, currency)
        trusted_cost = MoneyAmount(cost_amt, currency) if cost_amt is not None else None
        observations = self.repo.list_observations(tenant, product_id)
        fresh = False
        outlier = False
        prev = current.amount if current.amount > 0 else None
        for obs in observations:
            obs_at = datetime.fromisoformat(obs["observed_at"])
            if observation_is_fresh(obs_at, max_age_sec=policy.freshness_max_age_sec):
                fresh = True
            if is_outlier(prev, Decimal(obs["price"])):
                outlier = True
        if not observations:
            fresh = True  # policy may still deny via insufficient competitor data
        decision = evaluate_price_decision(
            decision_id=str(uuid.uuid4()),
            tenant_id=tenant,
            product_id=product_id,
            policy=policy,
            current=current,
            proposed=MoneyAmount(proposed_amount, currency),
            trusted_cost=trusted_cost,
            observations_fresh=fresh,
            outlier=outlier,
            price_version=price_version,
        )
        self.repo.save_decision(tenant, decision.decision_id, {
            "decision_id": decision.decision_id,
            "product_id": product_id,
            "outcome": decision.outcome,
            "proposed": str(decision.proposed_price.amount),
            "current": str(decision.current_price.amount),
            "currency": currency,
            "price_version": decision.price_version,
            "reasons": list(decision.reasons),
        })
        self.obs.emit("commerce.price.decision", metadata={"decision_id": decision.decision_id, "outcome": decision.outcome})
        return decision

    def apply_price_decision(
        self,
        *,
        tenant_id: str,
        decision_id: str,
        approval_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> PriceChangeReceipt:
        tenant = require_tenant_id(tenant_id)
        stored = self.repo.get_decision(tenant, decision_id)
        if stored is None:
            raise ProductPlatformError(COMMERCE_NOT_FOUND)
        if stored.get("applied"):
            return PriceChangeReceipt(
                receipt_id=str(uuid.uuid4()),
                tenant_id=tenant,
                decision_id=decision_id,
                previous_price=MoneyAmount(Decimal(stored["current"]), stored["currency"]),
                applied_price=MoneyAmount(Decimal(stored["proposed"]), stored["currency"]),
                status="idempotent",
            )
        outcome = stored["outcome"]
        if outcome == "NO_CHANGE":
            return PriceChangeReceipt(
                receipt_id=str(uuid.uuid4()),
                tenant_id=tenant,
                decision_id=decision_id,
                previous_price=MoneyAmount(Decimal(stored["current"]), stored["currency"]),
                applied_price=MoneyAmount(Decimal(stored["current"]), stored["currency"]),
                status="no_change",
            )
        if outcome == PRICE_DENY or outcome == "INSUFFICIENT_DATA":
            raise ProductPlatformError(COMMERCE_PRICE_DENIED)
        if outcome == PRICE_REQUIRE_APPROVAL:
            if not approval_id:
                raise ProductPlatformError("COMMERCE_APPROVAL_REQUIRED")
            approval = self._approvals.get(approval_id)
            if approval is None or approval.get("used"):
                raise ProductPlatformError(COMMERCE_APPROVAL_REPLAY)
            if approval.get("tenant_id") != tenant or approval.get("decision_id") != decision_id:
                raise ProductPlatformError(COMMERCE_APPROVAL_REPLAY)
            approval["used"] = True
        if outcome not in {PRICE_ALLOW, PRICE_REQUIRE_APPROVAL}:
            raise ProductPlatformError(COMMERCE_PRICE_DENIED)
        product_id = stored["product_id"]
        currency = stored["currency"]
        proposed = Decimal(stored["proposed"])
        previous = Decimal(stored["current"])
        expected_version = int(stored["price_version"])
        new_version = self.repo.set_price(
            tenant, product_id, currency, proposed, expected_version=expected_version if expected_version else None
        )
        binding = self.repo.get_cms_binding(tenant, product_id, self.cms.provider_id)
        external_ref = ""
        if binding:
            result = self.cms.update_price(
                tenant_id=tenant,
                external_id=binding["external_product_id"],
                price=proposed,
                currency=currency,
                decision_id=decision_id,
                idempotency_key=idempotency_key or decision_id,
                expected_etag=binding.get("etag"),
            )
            external_ref = result["external_id"]
        self.repo.mark_decision_applied(tenant, decision_id)
        receipt = PriceChangeReceipt(
            receipt_id=str(uuid.uuid4()),
            tenant_id=tenant,
            decision_id=decision_id,
            previous_price=MoneyAmount(previous, currency),
            applied_price=MoneyAmount(proposed, currency),
            external_ref=external_ref,
            status="applied",
        )
        self.obs.emit("commerce.price.applied", metadata={"decision_id": decision_id, "price_version": new_version})
        return receipt

    def grant_price_approval(self, *, tenant_id: str, decision_id: str) -> str:
        tenant = require_tenant_id(tenant_id)
        approval_id = str(uuid.uuid4())
        self._approvals[approval_id] = {"tenant_id": tenant, "decision_id": decision_id, "used": False}
        return approval_id

    def set_trusted_cost(self, *, tenant_id: str, product_id: str, amount: Decimal, currency: str = "RUB") -> None:
        tenant = require_tenant_id(tenant_id)
        self.repo.set_trusted_cost(tenant, product_id, currency, amount)

    def observe_stock(
        self,
        *,
        tenant_id: str,
        product_id: str,
        location_id: str,
        on_hand: Decimal,
        reserved: Decimal = Decimal("0"),
        source: str = "erp",
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        pos = self.repo.upsert_inventory(
            tenant, product_id, location_id, on_hand=on_hand, reserved=reserved, source=source
        )
        self.obs.emit("commerce.stock.observed", metadata={"product_id": product_id, "location": location_id})
        return {"available": str(pos.available), "on_hand": str(pos.on_hand), "reserved": str(pos.reserved)}

    def reserve_stock(
        self,
        *,
        tenant_id: str,
        product_id: str,
        location_id: str,
        quantity: Decimal,
        idempotency_key: str,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        reservation_id = str(uuid.uuid4())
        rid = self.repo.try_reserve(
            tenant,
            product_id,
            location_id,
            quantity,
            reservation_id=reservation_id,
            idempotency_key=idempotency_key,
        )
        self.obs.emit("commerce.stock.reserved", metadata={"reservation_id": rid})
        return {"reservation_id": rid, "status": "reserved"}

    def ingest_order(
        self,
        *,
        tenant_id: str,
        external_ref: str,
        source: str,
        items: list[dict],
        currency: str = "RUB",
    ) -> PlatformOrder:
        tenant = require_tenant_id(tenant_id)
        existing = self.repo.get_order_by_external(tenant, external_ref)
        if existing:
            return PlatformOrder(
                order_id=existing["order_id"],
                tenant_id=tenant,
                external_ref=external_ref,
                source=existing.get("source", source),
                currency=existing.get("currency", currency),
                status=existing.get("status", ORDER_NEW),
                items=(),
                order_total=MoneyAmount(Decimal(existing.get("order_total", "0")), currency),
            )
        order_id = str(uuid.uuid4())
        line_items: list[PlatformOrderItem] = []
        total = Decimal("0")
        for row in items:
            qty = Decimal(str(row["quantity"]))
            unit = Decimal(str(row["unit_price"]))
            line_total = qty * unit
            total += line_total
            line_items.append(
                PlatformOrderItem(
                    line_id=str(uuid.uuid4()),
                    product_id=str(row.get("product_id") or ""),
                    variant_id=str(row.get("variant_id") or ""),
                    sku=str(row.get("sku") or ""),
                    quantity=qty,
                    unit_price=MoneyAmount(unit, currency),
                    line_total=MoneyAmount(line_total, currency),
                )
            )
        computed_total = sum((i.line_total.amount for i in line_items), Decimal("0"))
        declared_total = items[0].get("order_total") if items else None
        if declared_total is not None and Decimal(str(declared_total)) != computed_total:
            raise ProductPlatformError(COMMERCE_ORDER_INVALID, "order total mismatch")
        order = PlatformOrder(
            order_id=order_id,
            tenant_id=tenant,
            external_ref=external_ref,
            source=source,
            currency=currency,
            status=ORDER_NEW,
            items=tuple(line_items),
            order_total=MoneyAmount(computed_total, currency),
        )
        payload = {
            "order_id": order_id,
            "tenant_id": tenant,
            "external_ref": external_ref,
            "source": source,
            "currency": currency,
            "status": ORDER_NEW,
            "order_total": str(computed_total),
            "items": [
                {
                    "line_id": i.line_id,
                    "product_id": i.product_id,
                    "sku": i.sku,
                    "quantity": str(i.quantity),
                    "unit_price": str(i.unit_price.amount),
                    "line_total": str(i.line_total.amount),
                }
                for i in line_items
            ],
            "version": 1,
        }
        self.repo.save_order(tenant, order_id, external_ref, payload, ORDER_NEW)
        self.obs.emit("commerce.order.ingested", metadata={"order_id": order_id})
        return order

    def transition_order(
        self,
        *,
        tenant_id: str,
        order_id: str,
        new_status: str,
        external_event_id: str = "",
        external_sequence: int | None = None,
        external_timestamp: str = "",
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        result = self.repo.transition_order_with_sequence(
            tenant,
            order_id,
            new_status=new_status,
            external_event_id=external_event_id,
            external_sequence=external_sequence,
            external_timestamp=external_timestamp,
        )
        self.obs.emit("commerce.order.transitioned", metadata={"order_id": order_id, "status": new_status})
        return result

    def cms_create_product(
        self,
        *,
        tenant_id: str,
        product_id: str,
        version_id: str,
        idempotency_key: str,
        capabilities: tuple[str, ...] = (),
    ) -> CmsOperationResult:
        tenant = require_tenant_id(tenant_id)
        from commerce.capabilities import CAP_CATALOG_WRITE

        if CAP_CATALOG_WRITE not in capabilities:
            raise ProductPlatformError(COMMERCE_ACCESS_DENIED)
        result = self.cms.create_product(
            tenant_id=tenant,
            product_id=product_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
        )
        binding_id = str(uuid.uuid4())
        self.repo.save_cms_binding(
            tenant,
            binding_id,
            {
                "binding_id": binding_id,
                "tenant_id": tenant,
                "product_id": product_id,
                "version_id": version_id,
                "system": self.cms.provider_id,
                "external_product_id": result["external_id"],
                "etag": 1,
            },
        )
        self.obs.emit("commerce.cms.operation.completed", metadata={"operation": "create", "external_id": result["external_id"]})
        return CmsOperationResult(
            operation_id=str(uuid.uuid4()),
            tenant_id=tenant,
            operation="create",
            status=result["status"],
            external_id=result["external_id"],
            verified=result.get("verified", {}),
        )

    def cms_update_price_raw(
        self,
        *,
        tenant_id: str,
        external_id: str,
        price: Decimal,
        capabilities: tuple[str, ...] = (),
    ) -> None:
        """Must fail — raw price update without decision."""
        from commerce.capabilities import CAP_PRICING_WRITE

        tenant = require_tenant_id(tenant_id)
        if CAP_PRICING_WRITE not in capabilities:
            raise ProductPlatformError(COMMERCE_ACCESS_DENIED)
        raise ProductPlatformError(COMMERCE_PRICE_DENIED, "price update requires PriceDecision")

    def cms_update_stock(
        self,
        *,
        tenant_id: str,
        product_id: str,
        location_id: str = "main",
        idempotency_key: str,
        capabilities: tuple[str, ...] = (),
        expected_inventory_version: int | None = None,
    ) -> CmsOperationResult:
        """Governed CMS stock sync from trusted InventoryPosition only."""
        tenant = require_tenant_id(tenant_id)
        from commerce.capabilities import CAP_STOCK_WRITE

        if CAP_STOCK_WRITE not in capabilities:
            raise ProductPlatformError(COMMERCE_ACCESS_DENIED)
        inv = self.repo.get_inventory(tenant, product_id, location_id)
        if inv is None:
            raise ProductPlatformError(COMMERCE_NOT_FOUND)
        if expected_inventory_version is not None and int(inv["version"]) != expected_inventory_version:
            raise ProductPlatformError(COMMERCE_STOCK_STALE)
        binding = self.repo.get_cms_binding(tenant, product_id, self.cms.provider_id)
        if binding is None:
            raise ProductPlatformError(COMMERCE_NOT_FOUND)
        trusted_stock = inv["on_hand"] - inv["reserved"]
        result = self.cms.update_stock(
            tenant_id=tenant,
            external_id=binding["external_product_id"],
            stock=trusted_stock,
            idempotency_key=idempotency_key,
            expected_etag=binding.get("etag"),
        )
        self.repo.save_cms_binding(
            tenant,
            binding.get("binding_id", str(uuid.uuid4())),
            {**binding, "etag": result.get("verified", {}).get("etag", binding.get("etag", 1))},
        )
        self.obs.emit(
            "commerce.cms.operation.completed",
            metadata={"operation": "stock_update", "product_id": product_id},
        )
        return CmsOperationResult(
            operation_id=str(uuid.uuid4()),
            tenant_id=tenant,
            operation="stock_update",
            status=result["status"],
            external_id=result["external_id"],
            verified=result.get("verified", {}),
        )

    def cms_update_product(
        self,
        *,
        tenant_id: str,
        product_id: str,
        version_id: str,
        idempotency_key: str,
        capabilities: tuple[str, ...] = (),
    ) -> CmsOperationResult:
        tenant = require_tenant_id(tenant_id)
        from commerce.capabilities import CAP_CATALOG_WRITE

        if CAP_CATALOG_WRITE not in capabilities:
            raise ProductPlatformError(COMMERCE_ACCESS_DENIED)
        version = self.repo.get_product_version(tenant, version_id)
        if version is None or version.get("product_id") != product_id:
            raise ProductPlatformError(COMMERCE_NOT_FOUND)
        binding = self.repo.get_cms_binding(tenant, product_id, self.cms.provider_id)
        if binding is None:
            raise ProductPlatformError(COMMERCE_NOT_FOUND)
        result = self.cms.update_product(
            tenant_id=tenant,
            external_id=binding["external_product_id"],
            version_id=version_id,
            idempotency_key=idempotency_key,
            expected_etag=binding.get("etag"),
        )
        self.repo.save_cms_binding(
            tenant,
            binding["binding_id"],
            {**binding, "version_id": version_id, "etag": result.get("verified", {}).get("etag", binding.get("etag", 1))},
        )
        self.obs.emit("commerce.cms.operation.completed", metadata={"operation": "update", "product_id": product_id})
        return CmsOperationResult(
            operation_id=str(uuid.uuid4()),
            tenant_id=tenant,
            operation="update",
            status=result["status"],
            external_id=result["external_id"],
            verified=result.get("verified", {}),
        )

    def cms_archive_product(
        self,
        *,
        tenant_id: str,
        product_id: str,
        idempotency_key: str,
        capabilities: tuple[str, ...] = (),
    ) -> CmsOperationResult:
        tenant = require_tenant_id(tenant_id)
        from commerce.capabilities import CAP_CATALOG_WRITE

        if CAP_CATALOG_WRITE not in capabilities:
            raise ProductPlatformError(COMMERCE_ACCESS_DENIED)
        binding = self.repo.get_cms_binding(tenant, product_id, self.cms.provider_id)
        if binding is None:
            raise ProductPlatformError(COMMERCE_NOT_FOUND)
        result = self.cms.archive_product(
            tenant_id=tenant,
            external_id=binding["external_product_id"],
            idempotency_key=idempotency_key,
            expected_etag=binding.get("etag"),
        )
        self.obs.emit("commerce.cms.operation.completed", metadata={"operation": "archive", "product_id": product_id})
        return CmsOperationResult(
            operation_id=str(uuid.uuid4()),
            tenant_id=tenant,
            operation="archive",
            status=result["status"],
            external_id=result["external_id"],
            verified=result.get("verified", {}),
        )

    def bulk_reprice(
        self,
        *,
        tenant_id: str,
        product_ids: list[str],
        proposed_by_product: dict[str, Decimal],
        bulk: bool = False,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        assert_sync_commerce_allowed(reprice_count=len(product_ids), bulk=bulk)
        results = []
        for pid in product_ids:
            decision = self.decide_price(
                tenant_id=tenant,
                product_id=pid,
                proposed_amount=proposed_by_product.get(pid, Decimal("0")),
            )
            results.append({"product_id": pid, "outcome": decision.outcome})
        return {"results": results, "count": len(results)}

    def start_bulk_reprice_apply(
        self,
        *,
        tenant_id: str,
        decision_ids: list[str],
        bulk: bool = False,
        job_id: str | None = None,
        cancelled: bool = False,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        assert_sync_commerce_allowed(reprice_count=len(decision_ids), bulk=bulk)
        jid = job_id or str(uuid.uuid4())
        job = self.repo.get_commerce_job(tenant, jid) or {
            "job_id": jid,
            "operation": "bulk_reprice_apply",
            "checkpoint": 0,
            "total": len(decision_ids),
            "decision_ids": decision_ids,
            "counts": {"applied": 0, "no_change": 0, "denied": 0, "approval_required": 0, "stale": 0, "failed": 0},
            "status": "running",
            "results": [],
        }
        if cancelled or job.get("cancelled"):
            job["status"] = "cancelled"
            self.repo.save_commerce_job(tenant, jid, job)
            return {"job_id": jid, "status": "cancelled"}
        start = int(job.get("checkpoint", 0))
        end = min(start + BULK_APPLY_BATCH_SIZE, len(decision_ids))
        for idx in range(start, end):
            did = decision_ids[idx]
            try:
                stored = self.repo.get_decision(tenant, did)
                if stored is None:
                    job["counts"]["failed"] += 1
                    job["results"].append({"decision_id": did, "status": "NOT_FOUND"})
                    continue
                outcome = stored["outcome"]
                if outcome in {PRICE_DENY, "INSUFFICIENT_DATA"}:
                    job["counts"]["denied"] += 1
                    job["results"].append({"decision_id": did, "status": "DENIED"})
                    continue
                if outcome == PRICE_REQUIRE_APPROVAL:
                    job["counts"]["approval_required"] += 1
                    job["results"].append({"decision_id": did, "status": "REQUIRE_APPROVAL"})
                    continue
                if outcome == "NO_CHANGE":
                    job["counts"]["no_change"] += 1
                    job["results"].append({"decision_id": did, "status": "NO_CHANGE"})
                    continue
                receipt = self.apply_price_decision(
                    tenant_id=tenant,
                    decision_id=did,
                    idempotency_key=f"bulk-{jid}-{did}",
                )
                job["counts"]["applied"] += 1
                job["results"].append({"decision_id": did, "status": receipt.status})
            except ProductPlatformError as exc:
                if exc.code == COMMERCE_PRICE_STALE_DECISION:
                    job["counts"]["stale"] += 1
                    job["results"].append({"decision_id": did, "status": "STALE_DECISION"})
                else:
                    job["counts"]["failed"] += 1
                    job["results"].append({"decision_id": did, "status": exc.code})
        job["checkpoint"] = end
        job["status"] = "completed" if end >= len(decision_ids) else "partial"
        self.repo.save_commerce_job(tenant, jid, job)
        return {"job_id": jid, "status": job["status"], "checkpoint": end, "counts": dict(job["counts"])}

    def start_cms_bulk_sync(
        self,
        *,
        tenant_id: str,
        product_specs: list[dict],
        bulk: bool = False,
        job_id: str | None = None,
        sync_stock: bool = False,
        cancelled: bool = False,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        assert_sync_commerce_allowed(cms_writes=len(product_specs), bulk=bulk)
        jid = job_id or str(uuid.uuid4())
        job = self.repo.get_commerce_job(tenant, jid) or {
            "job_id": jid,
            "operation": "cms_bulk_sync",
            "checkpoint": 0,
            "total": len(product_specs),
            "specs": product_specs,
            "counts": {"created": 0, "updated": 0, "stock_synced": 0, "skipped": 0, "failed": 0},
            "status": "running",
            "results": [],
        }
        if cancelled or job.get("cancelled"):
            job["status"] = "cancelled"
            self.repo.save_commerce_job(tenant, jid, job)
            return {"job_id": jid, "status": "cancelled"}
        from commerce.capabilities import CAP_CATALOG_WRITE, CAP_STOCK_WRITE

        caps = (CAP_CATALOG_WRITE, CAP_STOCK_WRITE) if sync_stock else (CAP_CATALOG_WRITE,)
        start = int(job.get("checkpoint", 0))
        end = min(start + BULK_CMS_SYNC_BATCH_SIZE, len(product_specs))
        for idx in range(start, end):
            spec = product_specs[idx]
            pid = str(spec["product_id"])
            vid = str(spec["version_id"])
            try:
                binding = self.repo.get_cms_binding(tenant, pid, self.cms.provider_id)
                if binding is None:
                    result = self.cms_create_product(
                        tenant_id=tenant,
                        product_id=pid,
                        version_id=vid,
                        idempotency_key=f"sync-{jid}-{pid}",
                        capabilities=caps,
                    )
                    job["counts"]["created"] += 1
                    job["results"].append({"product_id": pid, "status": "CREATED", "external_id": result.external_id})
                else:
                    result = self.cms_update_product(
                        tenant_id=tenant,
                        product_id=pid,
                        version_id=vid,
                        idempotency_key=f"sync-up-{jid}-{pid}",
                        capabilities=(CAP_CATALOG_WRITE,),
                    )
                    job["counts"]["updated"] += 1
                    job["results"].append({"product_id": pid, "status": "UPDATED", "external_id": result.external_id})
                if sync_stock and spec.get("location_id"):
                    self.cms_update_stock(
                        tenant_id=tenant,
                        product_id=pid,
                        location_id=str(spec.get("location_id") or "main"),
                        idempotency_key=f"sync-st-{jid}-{pid}",
                        capabilities=(CAP_STOCK_WRITE,),
                    )
                    job["counts"]["stock_synced"] += 1
            except ProductPlatformError as exc:
                job["counts"]["failed"] += 1
                job["results"].append({"product_id": pid, "status": "PROVIDER_FAILED", "code": exc.code})
        job["checkpoint"] = end
        job["status"] = "completed" if end >= len(product_specs) else "partial"
        self.repo.save_commerce_job(tenant, jid, job)
        return {"job_id": jid, "status": job["status"], "checkpoint": end, "counts": dict(job["counts"])}
