"""Deterministic Bitrix FIXTURE adapter — rich catalog semantics."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.bitrix.catalog import GLOBAL_BITRIX_CATALOG, BitrixCatalogStore
from integrations.bitrix.errors import (
    BitrixAmbiguousTargetError,
    BitrixNotFoundError,
    BitrixUnsupportedCapabilityError,
    BitrixValidationError,
    BitrixWriteVerificationFailedError,
)
from integrations.bitrix.mapping import (
    build_preview,
    canonical_to_bitrix_payload,
    resolve_product_target,
    selective_export_filter,
    validate_create_payload,
)


@dataclass
class BitrixFixtureState(FixtureAdapterState):
    verification_mismatch: bool = False
    force_ambiguous: bool = False
    tenant_override: str = ""


class BitrixFixtureAdapter(FixtureProviderAdapter):
    """Bitrix-specific FIXTURE adapter with catalog, preview, verify-after-write."""

    def __init__(self, *, state: BitrixFixtureState | None = None, store: BitrixCatalogStore | None = None):
        super().__init__("bitrix", state=state or BitrixFixtureState())
        self.state: BitrixFixtureState = self.state  # type: ignore[assignment]
        self._store = store or GLOBAL_BITRIX_CATALOG
        self.environment = "FIXTURE"
        self.live = False

    def verify(self, *, credential_ref: str) -> dict:
        base = super().verify(credential_ref=credential_ref)
        if not base.get("ok"):
            return base
        return {
            **base,
            "provider_identity": "fixture:bitrix",
            "capabilities": [
                "cms.bitrix.catalog.read",
                "cms.bitrix.catalog.write",
                "bitrix.read",
                "bitrix.write",
            ],
            "aspro_profile": "aspro_premier_fixture_v1",
        }

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._raise_if_bad()
        params = params or {}
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(params.get("operation") or "").strip()

        if operation == "product_lookup":
            return self._read_product_lookup(tenant, params)
        if operation == "price_read":
            article = str(params.get("article") or params.get("sku") or "")
            out = self._store.read_price(tenant_id=tenant, article=article)
            if not out:
                raise BitrixNotFoundError("price_not_found")
            return out
        if operation == "stock_read":
            article = str(params.get("article") or params.get("sku") or "")
            out = self._store.read_stock(tenant_id=tenant, article=article)
            if not out:
                raise BitrixNotFoundError("stock_not_found")
            return out
        if operation == "order_read":
            page = int(params.get("page") or 1)
            return self._store.orders_page(tenant_id=tenant, page=page)

        # Default catalog list — backward compatible pagination
        page = int(params.get("page") or 1)
        if page > self.state.max_pages:
            return {"items": [], "next_page": None, "page": page, "bounded": True, "mode": "FIXTURE", "live": False}
        self.state.pages_served += 1
        out = self._store.list_products(tenant_id=tenant, page=page, page_size=2)
        self.state.reads.append({"capability": capability, "page": page, "tenant_id": tenant})
        return out

    def _read_product_lookup(self, tenant: str, params: dict) -> dict:
        if self.state.force_ambiguous:
            raise BitrixAmbiguousTargetError("forced_ambiguous")
        try:
            product = resolve_product_target(
                self._store,
                tenant_id=tenant,
                bitrix_id=str(params.get("bitrix_id") or ""),
                xml_id=str(params.get("xml_id") or ""),
                article=str(params.get("article") or params.get("sku") or ""),
                panda_product_id=str(params.get("panda_product_id") or ""),
                name=str(params.get("name") or ""),
                allow_name_only=bool(params.get("allow_name_only")),
            )
        except BitrixAmbiguousTargetError:
            raise
        except BitrixNotFoundError:
            raise
        return {"product": product, "mode": "FIXTURE", "live": False}

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

        handler = {
            "product_create": self._write_product_create,
            "product_update": self._write_product_update,
            "price_update": self._write_price_update,
            "stock_update": self._write_stock_update,
            "publish": self._write_publish,
            "selective_export": self._write_selective_export,
        }.get(operation)

        if handler is None:
            raise BitrixUnsupportedCapabilityError(f"unsupported_write:{operation}")

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

    def _write_product_create(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        product_in = dict(payload.get("product") or payload)
        aspro = bool(payload.get("aspro_premier_enabled"))
        mapped = canonical_to_bitrix_payload(product=product_in, aspro_enabled=aspro)
        validate_create_payload(mapped)
        preview = payload.get("preview") or build_preview(
            operation="product_create",
            before=None,
            after={"name": mapped.get("NAME"), "article": mapped.get("PROPERTY_ARTNUMBER")},
        )
        panda_id = str(payload.get("panda_product_id") or product_in.get("product_id") or "")
        created = self._store.create_product(
            tenant_id=tenant,
            payload={
                "name": mapped.get("NAME"),
                "article": mapped.get("PROPERTY_ARTNUMBER"),
                "description": mapped.get("DETAIL_TEXT"),
                "price": product_in.get("price") or product_in.get("amount"),
                "currency": product_in.get("currency") or "RUB",
                "active": bool(payload.get("active", False)),
                "properties": product_in.get("properties") or {},
            },
            panda_product_id=panda_id,
        )
        verified = self._verify_after_write(
            tenant=tenant,
            article=created.get("article") or "",
            expected={"name": created.get("name"), "active": created.get("active")},
        )
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "product_create",
            "mode": "FIXTURE",
            "live": False,
            "verified": verified,
            "idempotent": False,
            "preview": preview,
            "product": created,
            "mapping": {"panda_product_id": panda_id, "bitrix_id": created.get("external_product_id")},
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_product_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        if payload.get("ambiguous_target"):
            raise BitrixAmbiguousTargetError("ambiguous_product_target")
        target = resolve_product_target(
            self._store,
            tenant_id=tenant,
            bitrix_id=str(payload.get("bitrix_id") or ""),
            article=str(payload.get("article") or payload.get("sku") or ""),
            panda_product_id=str(payload.get("panda_product_id") or ""),
        )
        before = dict(target)
        changes = dict(payload.get("changes") or payload.get("updates") or {})
        if not changes and payload.get("name"):
            changes = {"name": payload.get("name")}
        updated = self._store.update_product(
            tenant_id=tenant,
            bitrix_id=target["external_product_id"],
            changes=changes,
        )
        preview = payload.get("preview") or build_preview(operation="product_update", before=before, after=updated)
        verified = self._verify_after_write(
            tenant=tenant,
            bitrix_id=target["external_product_id"],
            expected={"name": updated.get("name")},
        )
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "product_update",
            "mode": "FIXTURE",
            "live": False,
            "verified": verified,
            "idempotent": False,
            "preview": preview,
            "product": updated,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_price_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        article = str(payload.get("article") or payload.get("sku") or "")
        new_amount = str(payload.get("new_price") or payload.get("price") or payload.get("amount") or "")
        currency = str(payload.get("currency") or "RUB")
        price_type = str(payload.get("price_type") or "RETAIL")
        prod, old, new = self._store.set_price(
            tenant_id=tenant,
            article=article,
            new_amount=new_amount,
            currency=currency,
            price_type=price_type,
        )
        if not prod:
            raise BitrixNotFoundError("price_target_not_found")
        preview = payload.get("preview") or build_preview(
            operation="price_update",
            before={"article": article, "price": old},
            after={"article": article, "price": new},
        )
        verified = self._verify_after_write(
            tenant=tenant,
            article=article,
            expected={"amount": new_amount},
            price_check=True,
        )
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
            "price": {"article": article, "old": old, "new": new},
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_stock_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        article = str(payload.get("article") or payload.get("sku") or "")
        qty = int(payload.get("quantity") or payload.get("stock") or 0)
        prod, old = self._store.set_stock(tenant_id=tenant, article=article, quantity=qty)
        if not prod:
            raise BitrixNotFoundError("stock_target_not_found")
        preview = payload.get("preview") or build_preview(
            operation="stock_update",
            before={"article": article, "total": old},
            after={"article": article, "total": qty},
        )
        verified = self._verify_after_write(tenant=tenant, article=article, expected={"total": qty}, stock_check=True)
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
            "stock": {"article": article, "old": old, "new": qty},
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_publish(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        target = resolve_product_target(
            self._store,
            tenant_id=tenant,
            bitrix_id=str(payload.get("bitrix_id") or ""),
            article=str(payload.get("article") or payload.get("sku") or ""),
        )
        before = {"active": target.get("active")}
        published = self._store.publish(tenant_id=tenant, bitrix_id=target["external_product_id"])
        preview = payload.get("preview") or build_preview(
            operation="publish",
            before=before,
            after={"active": published.get("active")},
        )
        verified = self._verify_after_write(
            tenant=tenant,
            bitrix_id=target["external_product_id"],
            expected={"active": True},
        )
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "publish",
            "mode": "FIXTURE",
            "live": False,
            "verified": verified,
            "idempotent": False,
            "preview": preview,
            "product": published,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_selective_export(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        all_products = list(payload.get("products") or [])
        selected = list(payload.get("selected") or payload.get("selected_skus") or [])
        filtered = selective_export_filter(all_products=all_products, selected=selected)
        previews = [
            build_preview(
                operation="selective_export",
                before=None,
                after={"article": p.get("sku") or p.get("article"), "name": p.get("title") or p.get("name")},
            )
            for p in filtered
        ]
        created = []
        for p in filtered:
            created.append(
                self._store.create_product(
                    tenant_id=tenant,
                    payload={
                        "name": p.get("title") or p.get("name"),
                        "article": p.get("sku") or p.get("article"),
                        "price": p.get("price"),
                        "active": False,
                    },
                    panda_product_id=str(p.get("product_id") or ""),
                )
            )
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "selective_export",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "exported_count": len(created),
            "skipped_count": len(all_products) - len(filtered),
            "previews": previews,
            "products": created,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _verify_after_write(
        self,
        *,
        tenant: str,
        bitrix_id: str = "",
        article: str = "",
        expected: dict,
        price_check: bool = False,
        stock_check: bool = False,
    ) -> str:
        if self.state.verification_mismatch:
            return "VERIFICATION_FAILED"
        if price_check and article:
            observed = self._store.read_price(tenant_id=tenant, article=article)
            if str((observed.get("price") or {}).get("amount")) != str(expected.get("amount")):
                raise BitrixWriteVerificationFailedError("price_verification_failed")
            return "VERIFIED"
        if stock_check and article:
            observed = self._store.read_stock(tenant_id=tenant, article=article)
            if int(observed.get("total") or -1) != int(expected.get("total")):
                raise BitrixWriteVerificationFailedError("stock_verification_failed")
            return "VERIFIED"
        if bitrix_id:
            prod = resolve_product_target(self._store, tenant_id=tenant, bitrix_id=bitrix_id)
            for k, v in expected.items():
                if prod.get(k) != v:
                    raise BitrixWriteVerificationFailedError("product_verification_failed")
            return "VERIFIED"
        if article:
            prod = resolve_product_target(self._store, tenant_id=tenant, article=article)
            for k, v in expected.items():
                if prod.get(k) != v:
                    raise BitrixWriteVerificationFailedError("product_verification_failed")
            return "VERIFIED"
        return "VERIFIED"


class AsproFixtureAdapter(BitrixFixtureAdapter):
    """Aspro Premier profile over Bitrix — not a separate commerce core."""

    def __init__(self, *, state: BitrixFixtureState | None = None, store: BitrixCatalogStore | None = None):
        super().__init__(state=state, store=store)
        self.provider_id = "aspro"

    def verify(self, *, credential_ref: str) -> dict:
        base = super().verify(credential_ref=credential_ref)
        if base.get("ok"):
            base["provider_identity"] = "fixture:aspro-over-bitrix"
            base["extends"] = "bitrix"
        return base
