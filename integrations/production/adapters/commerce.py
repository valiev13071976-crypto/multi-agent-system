"""Production commerce adapters — Bitrix / 1C / CRM via IntegrationService."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.observability import ProviderObservability


@dataclass
class ProductionCommerceAdapter:
    provider_id: str
    integration_service: Any
    tenant_id: str
    obs: ProviderObservability | None = None
    _write_receipts: dict[str, dict] = field(default_factory=dict)

    async def read(self, *, operation: str, params: dict[str, Any]) -> dict:
        started = time.monotonic()
        try:
            result = await self.integration_service.invoke(
                tenant_id=self.tenant_id,
                integration_id=f"{self.provider_id}_default",
                operation=operation,
                body=params,
                capability=f"{self.provider_id}.read",
                is_write=False,
            )
            if self.obs:
                self.obs.emit(provider_id=self.provider_id, operation=f"read:{operation}", success=True, latency_ms=(time.monotonic() - started) * 1000)
            return result.get("result", result)
        except Exception as exc:
            if self.obs:
                self.obs.emit(provider_id=self.provider_id, operation=f"read:{operation}", success=False, error_category=ProviderErrorCategory.PROVIDER_ERROR.value)
            raise ProductionProviderError(ProviderErrorCategory.PROVIDER_ERROR, message=str(type(exc).__name__), provider_id=self.provider_id) from exc

    async def write(self, *, operation: str, params: dict[str, Any], idempotency_key: str) -> dict:
        if idempotency_key in self._write_receipts:
            return self._write_receipts[idempotency_key]
        started = time.monotonic()
        try:
            result = await self.integration_service.invoke(
                tenant_id=self.tenant_id,
                integration_id=f"{self.provider_id}_default",
                operation=operation,
                body=params,
                capability=f"{self.provider_id}.write",
                is_write=True,
                idempotency_key=idempotency_key,
            )
            payload = result.get("result", result)
            receipt = {
                "receipt_id": str(payload.get("receipt_id") or result.get("operation_id") or f"rcpt-{uuid.uuid4().hex[:12]}"),
                "status": str(payload.get("status") or result.get("status") or "accepted"),
                "provider_reference": str(payload.get("provider_reference") or ""),
            }
            self._write_receipts[idempotency_key] = receipt
            if self.obs:
                self.obs.emit(provider_id=self.provider_id, operation=f"write:{operation}", success=True, latency_ms=(time.monotonic() - started) * 1000)
            return receipt
        except Exception as exc:
            if self.obs:
                self.obs.emit(provider_id=self.provider_id, operation=f"write:{operation}", success=False, error_category=ProviderErrorCategory.PROVIDER_ERROR.value)
            raise ProductionProviderError(ProviderErrorCategory.PROVIDER_ERROR, message=str(type(exc).__name__), provider_id=self.provider_id) from exc

    def health_check(self) -> dict:
        foundations = {}
        try:
            foundations = self.integration_service.provider_foundations()
        except Exception:
            pass
        contract = foundations.get(self.provider_id)
        if contract is None:
            return {"status": "unknown", "provider": self.provider_id}
        return {"status": "configured", "provider": self.provider_id, "adapter_id": contract.adapter_id}


@dataclass
class SandboxCommerceAdapter:
    """Deterministic sandbox adapter for tests without live ERP credentials."""

    provider_id: str
    _write_receipts: dict[str, dict] = field(default_factory=dict)
    catalog: dict[str, dict] = field(default_factory=dict)

    async def read(self, *, operation: str, params: dict[str, Any]) -> dict:
        if operation in {"product.get", "catalog.read"}:
            sku = str(params.get("sku") or "default")
            return {"sku": sku, "name": self.catalog.get(sku, {}).get("name", "Product"), "stock": self.catalog.get(sku, {}).get("stock", 0)}
        if operation in {"stock.read", "price.read"}:
            sku = str(params.get("sku") or "default")
            item = self.catalog.get(sku, {"stock": 10, "price_minor": 1000})
            return {"sku": sku, **item}
        return {"operation": operation, "rows": []}

    async def write(self, *, operation: str, params: dict[str, Any], idempotency_key: str) -> dict:
        if idempotency_key in self._write_receipts:
            return self._write_receipts[idempotency_key]
        sku = str(params.get("sku") or "default")
        if operation in {"stock.update", "price.update", "order.status"}:
            self.catalog.setdefault(sku, {})
            if "stock" in params:
                self.catalog[sku]["stock"] = int(params["stock"])
            if "price_minor" in params:
                self.catalog[sku]["price_minor"] = int(params["price_minor"])
        receipt = {"receipt_id": f"rcpt-{uuid.uuid4().hex[:12]}", "status": "accepted", "provider_reference": f"{self.provider_id}:{sku}"}
        self._write_receipts[idempotency_key] = receipt
        return receipt

    def health_check(self) -> dict:
        return {"status": "healthy", "provider": self.provider_id, "mode": "sandbox"}


def build_commerce_adapters(env: dict, integration_service: Any | None = None) -> dict[str, Any]:
    enabled = str(env.get("COMMERCE_INTEGRATIONS_ENABLED") or "true").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return {}
    out: dict[str, Any] = {}
    for pid in ("bitrix", "onec", "crm"):
        configured = bool(str(env.get(f"{pid.upper()}_ENABLED") or env.get(f"{pid.upper()}_WEBHOOK_URL") or env.get(f"{pid.upper()}_API_URL") or "").strip())
        if integration_service is not None and configured:
            out[pid] = ProductionCommerceAdapter(provider_id=pid, integration_service=integration_service, tenant_id="platform")
        else:
            out[pid] = SandboxCommerceAdapter(provider_id=pid)
    return out
