"""Deterministic Wildberries FIXTURE adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.wildberries.catalog import GLOBAL_WB_CATALOG, WildberriesCatalogStore
from integrations.wildberries.errors import (
    WildberriesAmbiguousTargetError,
    WildberriesNotFoundError,
    WildberriesPriceFloorError,
    WildberriesUncertainWriteOutcomeError,
    WildberriesUnsupportedCapabilityError,
    WildberriesWriteVerificationFailedError,
)
from integrations.wildberries.mapping import (
    build_preview,
    enforce_price_floor,
    map_category,
    resolve_card_target,
    selective_rows,
)
from marketplace.models import PROMO_PLATFORM, MarketplacePromotionObservation, PROVIDER_WILDBERRIES, MoneyAmount
from marketplace.economics import assess_promotion_risk, calculate_profitability
from marketplace.models import MarketplaceCommissionObservation


@dataclass
class WildberriesFixtureState(FixtureAdapterState):
    verification_mismatch: bool = False
    force_ambiguous: bool = False
    uncertain_write: bool = False
    tenant_override: str = ""


class WildberriesFixtureAdapter(FixtureProviderAdapter):
    def __init__(self, *, state: WildberriesFixtureState | None = None, store: WildberriesCatalogStore | None = None):
        super().__init__("wildberries", state=state or WildberriesFixtureState())
        self.state: WildberriesFixtureState = self.state  # type: ignore[assignment]
        self._store = store or GLOBAL_WB_CATALOG
        self.environment = "FIXTURE"
        self.live = False

    def verify(self, *, credential_ref: str) -> dict:
        base = super().verify(credential_ref=credential_ref)
        if not base.get("ok"):
            return base
        return {
            **base,
            "provider_identity": "fixture:wildberries",
            "capabilities": [
                "marketplace.product",
                "marketplace.wb.stock.read",
                "marketplace.wb.price.read",
                "marketplace.wb.price.write",
                "marketplace.wb.orders.read",
            ],
        }

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._raise_if_bad()
        params = params or {}
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(params.get("operation") or "").strip()

        if operation == "card_lookup":
            return self._read_card_lookup(tenant, params)
        if operation == "price_read":
            article = str(params.get("seller_article") or params.get("sku") or params.get("article") or "")
            out = self._store.read_price(tenant_id=tenant, seller_article=article)
            if not out:
                raise WildberriesNotFoundError("price_not_found")
            return out
        if operation == "stock_read":
            article = str(params.get("seller_article") or params.get("sku") or "")
            warehouse = str(params.get("warehouse") or "main")
            out = self._store.read_stock(tenant_id=tenant, seller_article=article, warehouse=warehouse)
            if not out:
                raise WildberriesNotFoundError("stock_not_found")
            return out
        if operation == "price_and_stock":
            article = str(params.get("seller_article") or params.get("sku") or "")
            return {
                "price": self._store.read_price(tenant_id=tenant, seller_article=article),
                "stock": self._store.read_stock(tenant_id=tenant, seller_article=article, warehouse=str(params.get("warehouse") or "main")),
                "mode": "FIXTURE",
                "live": False,
            }
        if operation == "order_read":
            return self._store.orders_page(tenant_id=tenant, page=int(params.get("page") or 1))
        if operation == "promotion_analysis":
            return self._promotion_analysis(tenant, params)

        page = int(params.get("page") or 1)
        if page > self.state.max_pages:
            return {"items": [], "next_page": None, "page": page, "bounded": True, "mode": "FIXTURE", "live": False}
        self.state.pages_served += 1
        return self._store.list_cards(tenant_id=tenant, page=page, page_size=2)

    def _read_card_lookup(self, tenant: str, params: dict) -> dict:
        if self.state.force_ambiguous:
            raise WildberriesAmbiguousTargetError("forced_ambiguous")
        card = resolve_card_target(
            self._store,
            tenant_id=tenant,
            nm_id=params.get("nm_id") or "",
            chrt_id=params.get("chrt_id") or "",
            seller_article=str(params.get("seller_article") or params.get("sku") or ""),
            barcode=str(params.get("barcode") or ""),
            panda_product_id=str(params.get("panda_product_id") or ""),
            name=str(params.get("name") or ""),
        )
        return {"card": card, "mode": "FIXTURE", "live": False}

    def _promotion_analysis(self, tenant: str, params: dict) -> dict:
        article = str(params.get("seller_article") or params.get("sku") or "WB-SKU-200")
        price = self._store.read_price(tenant_id=tenant, seller_article=article)
        card, _ = self._store.resolve_variant(tenant_id=tenant, seller_article=article)
        promo_data = card.get("platform_promo") if card else None
        if not promo_data:
            return {"risk": "SAFE", "ownership": None, "mutate": False, "alert": False}
        promo = MarketplacePromotionObservation(
            promotion_id="fixture-promo",
            provider=PROVIDER_WILDBERRIES,
            sku_id=article,
            ownership=str(promo_data.get("ownership") or PROMO_PLATFORM),
            displayed_price=MoneyAmount(Decimal(str(promo_data.get("buyer_visible_price") or "0")), "RUB"),
            seller_price=MoneyAmount(Decimal(str(promo_data.get("seller_price") or price.get("base_price") or "0")), "RUB"),
            platform_discount=None,
            seller_discount=None,
        )
        purchase = Decimal(str(card.get("purchase_cost") or "0"))
        econ = calculate_profitability(
            sku_id=article,
            provider=PROVIDER_WILDBERRIES,
            selling_price=Decimal(str(price.get("seller_effective_price") or "0")),
            purchase_cost=purchase,
            commission=MarketplaceCommissionObservation(
                observation_id="wb-fixture-comm",
                provider=PROVIDER_WILDBERRIES,
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

        if self.state.uncertain_write and operation in {"price_update", "card_create", "stock_update"}:
            raise WildberriesUncertainWriteOutcomeError("uncertain_write_outcome")

        handler = {
            "price_update": self._write_price_update,
            "stock_update": self._write_stock_update,
            "card_create": self._write_card_create,
            "card_update": self._write_card_update,
            "selective_export": self._write_selective_export,
        }.get(operation)
        if handler is None:
            raise WildberriesUnsupportedCapabilityError(f"unsupported_write:{operation}")

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
        article = str(payload.get("seller_article") or payload.get("sku") or "")
        new_amount = str(payload.get("new_price") or payload.get("price") or payload.get("amount") or "")
        proposed = Decimal(new_amount)
        if payload.get("skip_floor_check") is not True:
            floor = enforce_price_floor(
                store=self._store,
                tenant_id=tenant,
                seller_article=article,
                proposed_price=proposed,
            )
        else:
            floor = {"allowed": True}
        current = self._store.read_price(tenant_id=tenant, seller_article=article)
        preview = payload.get("preview") or build_preview(
            operation="price_update",
            before=current,
            after={"seller_article": article, "base_price": new_amount},
        )
        _, old, new = self._store.set_price(tenant_id=tenant, seller_article=article, new_amount=new_amount)
        verified = self._verify_price(tenant, article, new_amount)
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

    def _write_stock_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        article = str(payload.get("seller_article") or payload.get("sku") or "")
        warehouse = str(payload.get("warehouse") or "")
        if not warehouse:
            raise WildberriesAmbiguousTargetError("warehouse_required")
        qty = int(payload.get("quantity") or payload.get("stock") or 0)
        try:
            self._store.warehouse_id(tenant, warehouse)
        except KeyError as exc:
            raise WildberriesAmbiguousTargetError("warehouse_not_mapped") from exc
        current = self._store.read_stock(tenant_id=tenant, seller_article=article, warehouse=warehouse)
        preview = payload.get("preview") or build_preview(
            operation="stock_update",
            before=current,
            after={"warehouse": warehouse, "quantity": qty},
        )
        card, old = self._store.set_stock(tenant_id=tenant, seller_article=article, warehouse=warehouse, quantity=qty)
        if not card:
            raise WildberriesNotFoundError("stock_target_not_found")
        observed = self._store.read_stock(tenant_id=tenant, seller_article=article, warehouse=warehouse)
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

    def _write_card_create(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        product = dict(payload.get("product") or payload)
        cat_id = str(product.get("category_id") or product.get("canonical_category_id") or "")
        if cat_id:
            product["subject_id"] = map_category(canonical_category_id=cat_id)
        if not product.get("seller_article") and not product.get("sku"):
            raise WildberriesNotFoundError("seller_article_required")
        preview = payload.get("preview") or build_preview(
            operation="card_create",
            before=None,
            after={"seller_article": product.get("seller_article") or product.get("sku"), "title": product.get("title")},
        )
        card = self._store.create_card(
            tenant_id=tenant,
            payload=product,
            panda_product_id=str(payload.get("panda_product_id") or ""),
        )
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "card_create",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED" if not self.state.verification_mismatch else "VERIFICATION_FAILED",
            "idempotent": False,
            "preview": preview,
            "card": card,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_card_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        if payload.get("ambiguous_target"):
            raise WildberriesAmbiguousTargetError("ambiguous_card_target")
        card = resolve_card_target(
            self._store,
            tenant_id=tenant,
            seller_article=str(payload.get("seller_article") or payload.get("sku") or ""),
            nm_id=payload.get("nm_id") or "",
        )
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "card_update",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "card": card,
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
                            "seller_article": r.get("sku") or r.get("seller_article"),
                            "new_price": r.get("price"),
                            "skip_floor_check": payload.get("skip_floor_check"),
                        },
                        idempotency_key=f"{idempotency_key}:{r.get('sku')}",
                    )
                )
            except (WildberriesPriceFloorError, WildberriesNotFoundError) as exc:
                results.append({"status": "FAILED", "sku": r.get("sku"), "error": exc.code})
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

    def _verify_price(self, tenant: str, article: str, expected: str) -> str:
        if self.state.verification_mismatch:
            return "VERIFICATION_FAILED"
        observed = self._store.read_price(tenant_id=tenant, seller_article=article)
        if str(observed.get("base_price")) != str(expected):
            raise WildberriesWriteVerificationFailedError("price_verification_failed")
        return "VERIFIED"
