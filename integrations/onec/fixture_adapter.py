"""Deterministic 1C FIXTURE adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.onec.catalog import GLOBAL_ONEC_CATALOG, OneCCatalogStore
from integrations.onec.errors import (
    OneCAmbiguousTargetError,
    OneCNotFoundError,
    OneCUnsupportedCapabilityError,
    OneCUncertainWriteOutcomeError,
    OneCWriteVerificationFailedError,
)
from integrations.onec.mapping import (
    build_preview,
    resolve_nomenclature_target,
    selective_rows,
    validate_document_payload,
)


@dataclass
class OneCFixtureState(FixtureAdapterState):
    verification_mismatch: bool = False
    force_ambiguous: bool = False
    uncertain_write: bool = False
    tenant_override: str = ""


class OneCFixtureAdapter(FixtureProviderAdapter):
    def __init__(self, *, state: OneCFixtureState | None = None, store: OneCCatalogStore | None = None):
        super().__init__("onec", state=state or OneCFixtureState())
        self.state: OneCFixtureState = self.state  # type: ignore[assignment]
        self._store = store or GLOBAL_ONEC_CATALOG
        self.environment = "FIXTURE"
        self.live = False

    def verify(self, *, credential_ref: str) -> dict:
        base = super().verify(credential_ref=credential_ref)
        if not base.get("ok"):
            return base
        return {
            **base,
            "provider_identity": "fixture:onec",
            "capabilities": [
                "erp.1c",
                "erp.1c.catalog.read",
                "erp.1c.catalog.write",
                "onec.read",
                "onec.write",
            ],
            "transport": "http_rest",
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
            out = self._store.read_price(tenant_id=tenant, article=article, price_type=str(params.get("price_type") or "RETAIL"))
            if not out:
                raise OneCNotFoundError("price_not_found")
            return out
        if operation == "stock_read":
            article = str(params.get("article") or params.get("sku") or "")
            warehouse = str(params.get("warehouse") or "main")
            out = self._store.read_stock(tenant_id=tenant, article=article, warehouse=warehouse)
            if not out:
                raise OneCNotFoundError("stock_not_found")
            return out
        if operation == "order_read":
            page = int(params.get("page") or 1)
            return self._store.list_orders(tenant_id=tenant, page=page)
        if operation == "warehouse_list":
            return {"warehouses": list({"wh-main", "wh-east"}), "mode": "FIXTURE", "live": False}

        page = int(params.get("page") or 1)
        if page > self.state.max_pages:
            return {"items": [], "next_page": None, "page": page, "bounded": True, "mode": "FIXTURE", "live": False}
        self.state.pages_served += 1
        out = self._store.list_products(tenant_id=tenant, page=page, page_size=2)
        self.state.reads.append({"capability": capability, "page": page, "tenant_id": tenant})
        return out

    def _read_product_lookup(self, tenant: str, params: dict) -> dict:
        if self.state.force_ambiguous:
            raise OneCAmbiguousTargetError("forced_ambiguous")
        product = resolve_nomenclature_target(
            self._store,
            tenant_id=tenant,
            guid=str(params.get("guid") or ""),
            xml_id=str(params.get("xml_id") or ""),
            article=str(params.get("article") or params.get("sku") or ""),
            panda_product_id=str(params.get("panda_product_id") or ""),
            name=str(params.get("name") or ""),
            allow_name_only=bool(params.get("allow_name_only")),
        )
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

        if self.state.uncertain_write and operation in {"price_update", "document_create"}:
            raise OneCUncertainWriteOutcomeError("uncertain_write_outcome")

        handler = {
            "price_update": self._write_price_update,
            "document_create": self._write_document_create,
            "selective_export": self._write_selective_export,
        }.get(operation)

        if handler is None:
            if operation == "stock_update":
                raise OneCUnsupportedCapabilityError("stock_write_requires_document")
            raise OneCUnsupportedCapabilityError(f"unsupported_write:{operation}")

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
        article = str(payload.get("article") or payload.get("sku") or "")
        new_amount = str(payload.get("new_price") or payload.get("price") or payload.get("amount") or "")
        price_type = str(payload.get("price_type") or "RETAIL")
        currency = str(payload.get("currency") or "RUB")
        current = self._store.read_price(tenant_id=tenant, article=article, price_type=price_type)
        if not current:
            raise OneCNotFoundError("price_target_not_found")
        preview = payload.get("preview") or build_preview(
            operation="price_update",
            before={"article": article, "price": current},
            after={"article": article, "price": {"amount": new_amount, "currency": currency, "price_type": price_type}},
        )
        prod, old, new = self._store.set_price(
            tenant_id=tenant,
            article=article,
            new_amount=new_amount,
            price_type=price_type,
            currency=currency,
        )
        verified = self._verify_after_write(
            tenant=tenant,
            article=article,
            price_type=price_type,
            expected_amount=new_amount,
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

    def _write_document_create(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        doc_payload = dict(payload.get("document") or payload)
        validate_document_payload(doc_payload)
        preview = payload.get("preview") or build_preview(
            operation="document_create",
            before=None,
            after={"document_type": doc_payload.get("document_type"), "items_count": len(doc_payload.get("items") or [])},
        )
        doc = self._store.create_document(
            tenant_id=tenant,
            document_type=str(doc_payload.get("document_type") or "sales_order"),
            payload=doc_payload,
            idempotency_key=idempotency_key,
        )
        verified = "VERIFICATION_FAILED" if self.state.verification_mismatch else "VERIFIED"
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "document_create",
            "mode": "FIXTURE",
            "live": False,
            "verified": verified,
            "idempotent": False,
            "preview": preview,
            "document": doc,
            "posted": False,
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _write_selective_export(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        all_rows = list(payload.get("rows") or payload.get("products") or [])
        selected = list(payload.get("selected") or payload.get("selected_skus") or [])
        filtered = selective_rows(all_rows=all_rows, selected=selected)
        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "selective_export",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "exported_count": len(filtered),
            "skipped_count": len(all_rows) - len(filtered),
            "previews": [build_preview(operation="selective_export", before=None, after={"article": r.get("sku")}) for r in filtered],
            "external_write_count": self._store.record_write(idempotency_key),
        }

    def _verify_after_write(
        self,
        *,
        tenant: str,
        article: str = "",
        price_type: str = "RETAIL",
        expected_amount: str = "",
    ) -> str:
        if self.state.verification_mismatch:
            return "VERIFICATION_FAILED"
        if article and expected_amount:
            observed = self._store.read_price(tenant_id=tenant, article=article, price_type=price_type)
            if str(observed.get("amount")) != str(expected_amount):
                raise OneCWriteVerificationFailedError("price_verification_failed")
        return "VERIFIED"
