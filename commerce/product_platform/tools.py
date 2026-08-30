"""Tool platform adapter for Product Platform (Block 11)."""

from __future__ import annotations

from commerce.capabilities import (
    CAP_CATALOG_WRITE,
    CAP_ORDER_WRITE,
    CAP_PRICING_PROPOSE,
    CAP_PRICING_WRITE,
    CAP_STOCK_WRITE,
)
from commerce.product_platform.errors import ProductPlatformError
from commerce.product_platform.service import ProductPlatformService
from tools.errors import ToolAuthFailedError, ToolNotFoundError, ToolPermanentFailureError, ToolUnavailableError


class ProductPlatformToolAdapter:
    adapter_id = "product_platform"

    def __init__(self, service: ProductPlatformService | None = None, *, enabled: bool = False):
        self._service = service
        self._enabled = enabled and service is not None

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("commerce.catalog.") or tool_id.startswith("commerce.product.") or tool_id.startswith(
            "commerce.price."
        ) or tool_id.startswith("commerce.stock.") or tool_id.startswith("commerce.order.") or tool_id.startswith(
            "commerce.cms."
        )

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._enabled else ADAPTER_UNAVAILABLE

    def _caps(self, request) -> tuple[str, ...]:
        return tuple(getattr(request, "requested_capabilities", None) or ())

    def _require_cap(self, caps: tuple[str, ...], required: str) -> None:
        if required not in caps:
            raise ToolAuthFailedError("tool_permission_denied")

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        tenant = request.tenant_id
        args = dict(request.arguments or {})
        op = request.operation
        svc = self._service
        try:
            if op in {"get", "catalog_get"}:
                product = svc.get_product(tenant_id=tenant, product_id=str(args["product_id"]))
                return {"found": product is not None, "product": product}
            if op == "analyze" or request.tool_id == "commerce.catalog.analyze":
                return svc.analyze_catalog(tenant_id=tenant, profile=str(args.get("profile") or "marketplace"))
            if op == "order_get":
                return {"status": "ok"}
        except ProductPlatformError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolNotFoundError("operation_not_supported")

    async def execute_write(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        caps = self._caps(request)
        tenant = request.tenant_id
        args = dict(request.arguments or {})
        op = request.operation
        svc = self._service
        try:
            if op == "import_preview":
                result = svc.import_preview(tenant_id=tenant, rows=list(args.get("rows") or []))
                return {"import_id": result.import_id, "created": result.created, "dry_run": True}
            if op == "import":
                result = svc.import_products(
                    tenant_id=tenant,
                    rows=list(args.get("rows") or []),
                    dry_run=bool(args.get("dry_run", False)),
                    bulk=bool(args.get("bulk", False)),
                )
                return {"import_id": result.import_id, "created": result.created, "updated": result.updated}
            if op == "observe_price":
                return svc.observe_price(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    source=str(args.get("source") or "manual"),
                    amount=args["amount"],
                    currency=str(args.get("currency") or "RUB"),
                )
            if op == "decide_price":
                self._require_cap(caps, CAP_PRICING_PROPOSE)
                from decimal import Decimal

                decision = svc.decide_price(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    proposed_amount=Decimal(str(args["proposed_amount"])),
                    currency=str(args.get("currency") or "RUB"),
                )
                return {"decision_id": decision.decision_id, "outcome": decision.outcome}
            if op == "apply_price":
                self._require_cap(caps, CAP_PRICING_WRITE)
                receipt = svc.apply_price_decision(
                    tenant_id=tenant,
                    decision_id=str(args["decision_id"]),
                    approval_id=args.get("approval_id"),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                )
                return {"receipt_id": receipt.receipt_id, "status": receipt.status, "external_id": receipt.external_ref}
            if op == "cms_create":
                self._require_cap(caps, CAP_CATALOG_WRITE)
                result = svc.cms_create_product(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    version_id=str(args["version_id"]),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                    capabilities=caps,
                )
                return {"external_id": result.external_id, "status": result.status}
            if op == "cms_update_stock":
                self._require_cap(caps, CAP_STOCK_WRITE)
                if "stock" in args or "quantity" in args:
                    raise ToolAuthFailedError("tool_permission_denied")
                result = svc.cms_update_stock(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    location_id=str(args.get("location_id") or "main"),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                    capabilities=caps,
                    expected_inventory_version=args.get("expected_inventory_version"),
                )
                return {"external_id": result.external_id, "status": result.status, "verified": dict(result.verified)}
            if op == "cms_update_product":
                self._require_cap(caps, CAP_CATALOG_WRITE)
                result = svc.cms_update_product(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    version_id=str(args["version_id"]),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                    capabilities=caps,
                )
                return {"external_id": result.external_id, "status": result.status}
            if op == "cms_archive_product":
                self._require_cap(caps, CAP_CATALOG_WRITE)
                result = svc.cms_archive_product(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                    capabilities=caps,
                )
                return {"external_id": result.external_id, "status": result.status}
            if op == "cms_update_price":
                from decimal import Decimal

                svc.cms_update_price_raw(
                    tenant_id=tenant,
                    external_id=str(args["external_id"]),
                    price=Decimal(str(args["price"])),
                    capabilities=caps,
                )
                return {}
            if op == "ingest_order":
                order = svc.ingest_order(
                    tenant_id=tenant,
                    external_ref=str(args["external_ref"]),
                    source=str(args.get("source") or "external"),
                    items=list(args.get("items") or []),
                    currency=str(args.get("currency") or "RUB"),
                )
                return {"order_id": order.order_id, "status": order.status}
            if op == "reserve_stock":
                from decimal import Decimal

                return svc.reserve_stock(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    location_id=str(args.get("location_id") or "main"),
                    quantity=Decimal(str(args["quantity"])),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                )
        except ProductPlatformError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolNotFoundError("operation_not_supported")
