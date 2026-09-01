"""Deterministic Yandex Market FIXTURE adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.yandex_market.catalog import GLOBAL_YM_CATALOG, YandexMarketCatalogStore
from integrations.yandex_market.errors import (
    YandexMarketAmbiguousTargetError,
    YandexMarketFulfillmentBoundaryError,
    YandexMarketNotFoundError,
    YandexMarketPriceFloorError,
    YandexMarketUncertainWriteOutcomeError,
    YandexMarketUnsupportedCapabilityError,
    YandexMarketWriteVerificationFailedError,
)
from integrations.yandex_market.mapping import (
    assert_fulfillment_warehouse,
    build_preview,
    enforce_price_floor,
    map_category,
    resolve_offer_target,
    selective_rows,
)
from marketplace.economics import assess_promotion_risk, calculate_profitability
from marketplace.models import PROMO_PLATFORM, MarketplaceCommissionObservation, MarketplacePromotionObservation, MoneyAmount, PROVIDER_YANDEX_MARKET


@dataclass
class YandexMarketFixtureState(FixtureAdapterState):
    verification_mismatch: bool = False
    force_ambiguous: bool = False
    uncertain_write: bool = False
    tenant_override: str = ""
    submission_outcome: str = "SUCCEEDED"  # PUBLISHED | REJECTED | PROCESSING


class YandexMarketFixtureAdapter(FixtureProviderAdapter):
    def __init__(self, *, state: YandexMarketFixtureState | None = None, store: YandexMarketCatalogStore | None = None):
        super().__init__("yandex_market", state=state or YandexMarketFixtureState())
        self.state: YandexMarketFixtureState = self.state  # type: ignore[assignment]
        self._store = store or GLOBAL_YM_CATALOG
        self.environment = "FIXTURE"
        self.live = False

    def verify(self, *, credential_ref: str) -> dict:
        base = super().verify(credential_ref=credential_ref)
        if not base.get("ok"):
            return base
        return {
            **base,
            "provider_identity": "fixture:yandex_market",
            "capabilities": [
                "marketplace.product",
                "marketplace.yandex.stock.read",
                "marketplace.yandex.price.read",
                "marketplace.yandex.price.write",
                "marketplace.yandex.orders.read",
            ],
        }

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._raise_if_bad()
        params = params or {}
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(params.get("operation") or "").strip()

        if operation == "offer_lookup":
            return self._read_offer_lookup(tenant, params)
        if operation == "business_scope":
            scope = self._store.business_scope(tenant)
            return {**scope, "mode": "FIXTURE", "live": False}
        if operation == "price_read":
            sku = str(params.get("shop_sku") or params.get("sku") or params.get("offer_id") or "")
            campaign = str(params.get("campaign_id") or "")
            out = self._store.read_price(tenant_id=tenant, shop_sku=sku, campaign_id=campaign)
            if not out:
                raise YandexMarketNotFoundError("price_not_found")
            return out
        if operation == "stock_read":
            sku = str(params.get("shop_sku") or params.get("sku") or "")
            warehouse = str(params.get("warehouse") or "dbs_main")
            out = self._store.read_stock(tenant_id=tenant, shop_sku=sku, warehouse=warehouse)
            if not out:
                raise YandexMarketNotFoundError("stock_not_found")
            return out
        if operation == "price_and_stock":
            sku = str(params.get("shop_sku") or params.get("sku") or "")
            wh = str(params.get("warehouse") or "dbs_main")
            return {
                "price": self._store.read_price(tenant_id=tenant, shop_sku=sku),
                "stock": self._store.read_stock(tenant_id=tenant, shop_sku=sku, warehouse=wh),
                "mode": "FIXTURE",
                "live": False,
            }
        if operation == "order_read":
            return self._store.orders_page(tenant_id=tenant, page=int(params.get("page") or 1))
        if operation == "promotion_read":
            return self._store.promotions_page(tenant_id=tenant)
        if operation == "promotion_analysis":
            return self._promotion_analysis(tenant, params)
        if operation == "submission_status":
            return self._read_submission_status(params)

        page = int(params.get("page") or 1)
        if page > self.state.max_pages:
            return {"items": [], "next_page": None, "page": page, "bounded": True, "mode": "FIXTURE", "live": False}
        self.state.pages_served += 1
        return self._store.list_offers(tenant_id=tenant, page=page, page_size=2)

    def _read_offer_lookup(self, tenant: str, params: dict) -> dict:
        if self.state.force_ambiguous:
            raise YandexMarketAmbiguousTargetError("forced_ambiguous")
        offer = resolve_offer_target(
            self._store,
            tenant_id=tenant,
            business_id=params.get("business_id") or "",
            campaign_id=str(params.get("campaign_id") or ""),
            offer_id=str(params.get("offer_id") or ""),
            shop_sku=str(params.get("shop_sku") or params.get("sku") or ""),
            market_sku=str(params.get("market_sku") or ""),
            barcode=str(params.get("barcode") or ""),
            panda_product_id=str(params.get("panda_product_id") or ""),
            name=str(params.get("name") or ""),
        )
        return {"offer": offer, "mode": "FIXTURE", "live": False}

    def _read_submission_status(self, params: dict) -> dict:
        task_id = str(params.get("task_id") or "")
        task = self._store.get_submission_task(task_id)
        if not task:
            raise YandexMarketNotFoundError("submission_task_not_found")
        status = task.get("status") or "UNKNOWN"
        terminal = status in {"PUBLISHED", "REJECTED", "DISABLED"}
        return {
            "task_id": task_id,
            "status": status,
            "terminal": terminal,
            "market_sku": task.get("market_sku"),
            "offer_id": task.get("offer_id"),
            "business_id": task.get("business_id"),
            "campaign_id": task.get("campaign_id"),
            "mode": "FIXTURE",
            "live": False,
        }

    def _promotion_analysis(self, tenant: str, params: dict) -> dict:
        sku = str(params.get("shop_sku") or params.get("sku") or "YM-SKU-200")
        price = self._store.read_price(tenant_id=tenant, shop_sku=sku)
        offer, _ = self._store.resolve_target(tenant_id=tenant, shop_sku=sku)
        promo_data = offer.get("platform_promo") if offer else None
        if not promo_data:
            return {"risk": "SAFE", "ownership": None, "mutate": False, "alert": False, "mode": "FIXTURE", "live": False}
        promo = MarketplacePromotionObservation(
            promotion_id="fixture-ym-promo",
            provider=PROVIDER_YANDEX_MARKET,
            sku_id=sku,
            ownership=str(promo_data.get("ownership") or PROMO_PLATFORM),
            displayed_price=MoneyAmount(Decimal(str(promo_data.get("customer_visible_price") or "0")), "RUB"),
            seller_price=MoneyAmount(Decimal(str(promo_data.get("seller_price") or price.get("seller_price") or "0")), "RUB"),
            platform_discount=None,
            seller_discount=None,
        )
        purchase = Decimal(str(offer.get("purchase_cost") or "0"))
        econ = calculate_profitability(
            sku_id=sku,
            provider=PROVIDER_YANDEX_MARKET,
            selling_price=Decimal(str(price.get("seller_effective_price") or "0")),
            purchase_cost=purchase,
            commission=MarketplaceCommissionObservation(
                observation_id="ym-fixture-comm",
                provider=PROVIDER_YANDEX_MARKET,
                category="default",
                rate=Decimal("0.15"),
                fixed_fee=Decimal("0"),
            ),
            logistics=Decimal("100"),
        )
        risk = assess_promotion_risk(promo=promo, profitability=econ)
        return {
            **risk,
            "mutate": False,
            "alert": risk.get("risk") not in {"SAFE"},
            "provider_controlled": promo.ownership == PROMO_PLATFORM,
            "customer_visible_price": price.get("customer_visible_price"),
            "mode": "FIXTURE",
            "live": False,
        }

    def write(
        self,
        *,
        capability: str,
        payload: dict,
        idempotency_key: str,
        tenant_id: str = "",
        credential_ref: str = "",
    ) -> dict:
        self._raise_if_bad()
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(payload.get("operation") or "generic").strip()

        if idempotency_key in self.state.writes:
            cached = dict(self.state.writes[idempotency_key])
            cached["idempotent"] = True
            return cached

        if operation == "generic":
            return self._generic_write(capability, payload, idempotency_key)

        if operation == "promotion_write":
            raise YandexMarketUnsupportedCapabilityError("promotion_write_not_supported")

        if self.state.uncertain_write and operation in {"price_update", "offer_submission", "stock_update"}:
            raise YandexMarketUncertainWriteOutcomeError("uncertain_write_outcome")

        handler = {
            "price_update": self._write_price_update,
            "stock_update": self._write_stock_update,
            "offer_submission": self._write_offer_submission,
            "offer_update": self._write_offer_update,
            "selective_export": self._write_selective_export,
            "reconcile_price": self._write_reconcile_price,
        }.get(operation)
        if handler is None:
            raise YandexMarketUnsupportedCapabilityError(f"unsupported_write:{operation}")

        out = handler(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)
        self.state.writes[idempotency_key] = out
        return out

    def _generic_write(self, capability: str, payload: dict, idempotency_key: str) -> dict:
        out = {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "payload_summary": {"keys": sorted(payload.keys())},
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_price_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        sku = str(payload.get("shop_sku") or payload.get("sku") or payload.get("offer_id") or "")
        campaign = str(payload.get("campaign_id") or "")
        new_amount = str(payload.get("new_price") or payload.get("price") or payload.get("amount") or "")
        proposed = Decimal(new_amount)
        if payload.get("skip_floor_check") is not True:
            floor = enforce_price_floor(
                store=self._store,
                tenant_id=tenant,
                shop_sku=sku,
                proposed_price=proposed,
                campaign_id=campaign,
            )
        else:
            floor = {"allowed": True}
        current = self._store.read_price(tenant_id=tenant, shop_sku=sku, campaign_id=campaign)
        preview = payload.get("preview") or build_preview(
            operation="price_update",
            before=current,
            after={"shop_sku": sku, "seller_price": new_amount},
        )
        _, old, new = self._store.set_price(tenant_id=tenant, shop_sku=sku, new_amount=new_amount, campaign_id=campaign)
        verified = self._verify_price(tenant, sku, new_amount, campaign)
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "price_update",
            "mode": "FIXTURE",
            "live": False,
            "verified": verified,
            "idempotent": False,
            "preview": preview,
            "price": {"old": old, "new": new, "floor": floor},
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_reconcile_price(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        sku = str(payload.get("shop_sku") or payload.get("sku") or "")
        expected = str(payload.get("expected_price") or payload.get("new_price") or "")
        observed = self._store.read_price(tenant_id=tenant, shop_sku=sku)
        actual = str(observed.get("seller_price") or "")
        verified = "VERIFIED" if actual == expected else ("MISMATCH" if actual else "UNKNOWN")
        return {
            "status": "RECONCILED",
            "operation": "reconcile_price",
            "verified": verified,
            "observed": observed,
            "expected": expected,
            "mode": "FIXTURE",
            "live": False,
            "idempotent": False,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_stock_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        sku = str(payload.get("shop_sku") or payload.get("sku") or "")
        warehouse = str(payload.get("warehouse") or "")
        if not warehouse:
            raise YandexMarketAmbiguousTargetError("warehouse_required")
        offer, _ = self._store.resolve_target(tenant_id=tenant, shop_sku=sku)
        if not offer:
            raise YandexMarketNotFoundError("stock_target_not_found")
        try:
            assert_fulfillment_warehouse(fulfillment=str(offer.get("fulfillment") or "DBS"), warehouse=warehouse)
            self._store.warehouse_id(tenant, warehouse)
        except KeyError as exc:
            raise YandexMarketAmbiguousTargetError("warehouse_not_mapped") from exc
        except YandexMarketFulfillmentBoundaryError:
            raise
        qty = int(payload.get("quantity") or payload.get("stock") or 0)
        current = self._store.read_stock(tenant_id=tenant, shop_sku=sku, warehouse=warehouse)
        preview = payload.get("preview") or build_preview(
            operation="stock_update",
            before=current,
            after={"warehouse": warehouse, "quantity": qty},
        )
        offer, old = self._store.set_stock(tenant_id=tenant, shop_sku=sku, warehouse=warehouse, quantity=qty)
        if not offer:
            raise YandexMarketNotFoundError("stock_target_not_found")
        observed = self._store.read_stock(tenant_id=tenant, shop_sku=sku, warehouse=warehouse)
        verified = "VERIFIED" if int(observed.get("available") or -1) == qty and not self.state.verification_mismatch else "VERIFICATION_FAILED"
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "stock_update",
            "mode": "FIXTURE",
            "live": False,
            "verified": verified,
            "idempotent": False,
            "preview": preview,
            "stock": {"old": old, "new": qty, "warehouse": warehouse},
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_offer_submission(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        product = dict(payload.get("product") or payload)
        cat_id = str(product.get("category_id") or product.get("canonical_category_id") or "")
        if cat_id:
            product["category_id"] = map_category(canonical_category_id=cat_id)
        if not product.get("shop_sku") and not product.get("sku") and not product.get("offer_id"):
            raise YandexMarketNotFoundError("shop_sku_required")
        preview = payload.get("preview") or build_preview(
            operation="offer_submission",
            before=None,
            after={"offer_id": product.get("offer_id"), "title": product.get("title")},
        )
        task = self._store.create_submission_task(
            tenant_id=tenant,
            payload=product,
            panda_product_id=str(payload.get("panda_product_id") or ""),
            initial_status="SUBMITTED",
        )
        outcome = self.state.submission_outcome
        if outcome == "PROCESSING":
            self._store.advance_submission_task(task["task_id"], status="PROCESSING")
        elif outcome == "REJECTED":
            self._store.advance_submission_task(task["task_id"], status="REJECTED")
        elif outcome == "SUCCEEDED" or outcome == "PUBLISHED":
            self._store.finalize_submission_success(tenant_id=tenant, task_id=task["task_id"])
        return {
            "status": "SUBMITTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "offer_submission",
            "submission_status": task.get("status"),
            "task_id": task["task_id"],
            "mode": "FIXTURE",
            "live": False,
            "verified": "ACCEPTED_PENDING",
            "idempotent": False,
            "preview": preview,
            "terminal_success": False,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_offer_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        if payload.get("ambiguous_target"):
            raise YandexMarketAmbiguousTargetError("ambiguous_offer_target")
        offer = resolve_offer_target(
            self._store,
            tenant_id=tenant,
            shop_sku=str(payload.get("shop_sku") or payload.get("sku") or ""),
            offer_id=str(payload.get("offer_id") or ""),
            campaign_id=str(payload.get("campaign_id") or ""),
        )
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "offer_update",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "offer": offer,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_selective_export(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        rows = list(payload.get("rows") or payload.get("products") or [])
        selected = list(payload.get("selected") or payload.get("selected_skus") or [])
        filtered = selective_rows(all_rows=rows, selected=selected)
        results = []
        for r in filtered:
            try:
                results.append(
                    self._write_price_update(
                        tenant=tenant,
                        capability=capability,
                        payload={
                            "operation": "price_update",
                            "shop_sku": r.get("shop_sku") or r.get("sku") or r.get("offer_id"),
                            "new_price": r.get("price"),
                            "skip_floor_check": payload.get("skip_floor_check"),
                        },
                        idempotency_key=f"{idempotency_key}:{r.get('shop_sku') or r.get('sku')}",
                    )
                )
            except (YandexMarketPriceFloorError, YandexMarketNotFoundError) as exc:
                results.append({"status": "FAILED", "sku": r.get("shop_sku"), "error": exc.code})
        ok = sum(1 for x in results if x.get("status") == "WRITE_ACCEPTED")
        fail = len(results) - ok
        return {
            "status": "PARTIAL" if fail else "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "selective_export",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "exported_count": ok,
            "failed_count": fail,
            "skipped_count": len(rows) - len(filtered),
            "results": results,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _verify_price(self, tenant: str, sku: str, expected: str, campaign: str = "") -> str:
        if self.state.verification_mismatch:
            return "VERIFICATION_FAILED"
        observed = self._store.read_price(tenant_id=tenant, shop_sku=sku, campaign_id=campaign)
        if str(observed.get("seller_price")) != str(expected):
            raise YandexMarketWriteVerificationFailedError("price_verification_failed")
        return "VERIFIED"
