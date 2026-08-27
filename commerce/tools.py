"""Commerce tool adapters — ToolGateway only, capability/HITL protected writes."""

from __future__ import annotations

from commerce.capabilities import (
    CAP_COMMERCE_RECONCILE,
    CAP_EDO_PREPARE,
    CAP_EDO_READ,
    CAP_FISCAL_READ,
    CAP_INVENTORY_READ,
    CAP_INVENTORY_RESERVE,
    CAP_MARKING_READ,
    CAP_MARKING_TRANSFER,
    CAP_MARKING_WITHDRAW,
    CAP_ORDER_READ,
    CAP_ORDER_WRITE,
    CAP_SUPPLIER_READ,
    LLM_DEFAULT_DENY,
)
from commerce.errors import CapabilityDeniedError, CommerceError, TenantAccessDeniedError
from tools.errors import ToolAuthFailedError, ToolPermanentFailureError, ToolUnavailableError
from tools.models import ADAPTER_DEGRADED, ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE


class CommerceToolAdapter:
    adapter_id = "commerce"

    def __init__(self, service=None, *, enabled: bool = False):
        self._service = service
        self._enabled = enabled and service is not None

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("commerce.") or tool_id.startswith(
            ("inventory.", "supplier.", "edo.", "marking.", "fiscal.")
        )

    def health(self) -> str:
        if not self._enabled:
            return ADAPTER_UNAVAILABLE
        return ADAPTER_HEALTHY if self._service is not None else ADAPTER_DEGRADED

    def _caps(self, request) -> tuple[str, ...]:
        return tuple(getattr(request, "requested_capabilities", None) or ())

    async def execute_read(self, request, context) -> dict:
        if not self._enabled or self._service is None:
            raise ToolUnavailableError()
        svc = self._service
        args = dict(request.arguments or {})
        tenant = request.tenant_id
        op = request.operation
        tool = request.tool_id

        try:
            if tool == "commerce.order.read" or op == "order_read":
                order = svc._get_order(tenant, str(args.get("order_id") or ""))
                return {
                    "order_id": order.order_id,
                    "fulfillment_state": order.fulfillment_state,
                    "buyer_type": order.buyer_type,
                    "scenario": order.scenario,
                    "rule_version": order.rule_version,
                }
            if tool == "commerce.order.validate" or op == "order_validate":
                order = svc.validate_order(tenant, str(args.get("order_id") or ""))
                return {"order_id": order.order_id, "fulfillment_state": order.fulfillment_state}
            if tool == "inventory.read" or op == "inventory_read":
                pos = svc.read_inventory(
                    tenant_id=tenant,
                    product_ref=str(args.get("product_ref") or ""),
                    warehouse=str(args.get("warehouse") or "main"),
                )
                return {
                    "product_ref": pos.product_ref,
                    "warehouse": pos.warehouse,
                    "available": pos.available,
                    "reserved": pos.reserved,
                    "source": pos.source,
                    "stale": pos.is_stale(),
                }
            if tool == "supplier.read" or op == "supplier_read":
                if args.get("rank"):
                    return {"ranked": svc.rank_suppliers(tenant, price=float(args.get("price") or 0))}
                row = svc.store.get_supplier(tenant, str(args.get("supplier_id") or ""))
                return row or {}
            if tool == "edo.status" or op == "edo_status":
                conf = svc.edo.get_document_status(
                    tenant_id=tenant, document_external_id=str(args.get("document_id") or "")
                )
                return {"external_id": conf.external_id, "status": conf.status, "system": conf.system}
            if tool == "marking.status" or op == "marking_status":
                conf = svc.marking.read_status(
                    tenant_id=tenant, code_ref=str(args.get("code_ref") or "")
                )
                return {"code_ref": conf.external_id, "status": conf.status}
            if tool == "fiscal.status" or op == "fiscal_status":
                conf = svc.fiscal.get_receipt_status(
                    tenant_id=tenant, receipt_external_id=str(args.get("receipt_id") or "")
                )
                return {"receipt_id": conf.external_id, "status": conf.status}
            if tool == "commerce.reconcile" or op == "reconcile":
                return svc.reconcile_order(tenant, str(args.get("order_id") or ""))
        except TenantAccessDeniedError as exc:
            raise ToolAuthFailedError("integration_not_available") from exc
        except CommerceError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolUnavailableError()

    async def execute_write(self, request, context) -> dict:
        if not self._enabled or self._service is None:
            raise ToolUnavailableError()
        caps = self._caps(request)
        args = dict(request.arguments or {})
        tenant = request.tenant_id
        tool = request.tool_id
        op = request.operation
        svc = self._service

        # Default deny high-risk without explicit capability
        if tool in {"marking.withdraw", "marking.transfer"} or op in {"marking_withdraw", "marking_transfer"}:
            needed = CAP_MARKING_WITHDRAW if "withdraw" in (tool + op) else CAP_MARKING_TRANSFER
            if needed not in caps:
                raise ToolAuthFailedError("tool_permission_denied")
            if needed in LLM_DEFAULT_DENY and not args.get("hitl_approved"):
                raise ToolAuthFailedError("tool_permission_denied")

        try:
            if tool == "inventory.reserve" or op == "inventory_reserve":
                result = svc.reserve_order(
                    tenant,
                    str(args.get("order_id") or ""),
                    capabilities=caps,
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                )
                return {"status": result.status, "external_refs": dict(result.external_refs), "error": result.error}
            if tool == "inventory.release" or op == "inventory_release":
                conf = svc.inventory.release(
                    tenant_id=tenant,
                    reservation_external_id=str(args.get("reservation_id") or ""),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                )
                return {"status": conf.status, "external_id": conf.external_id}
            if tool == "edo.prepare" or op == "edo_prepare":
                if CAP_EDO_PREPARE not in caps:
                    raise ToolAuthFailedError("tool_permission_denied")
                conf = svc.edo.prepare_document(
                    tenant_id=tenant,
                    payload=dict(args.get("payload") or {}),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                )
                return {"document_id": conf.external_id, "status": conf.status}
            if tool == "marking.transfer" or op == "marking_transfer":
                conf = svc.marking.transfer(
                    tenant_id=tenant,
                    code_ref=str(args.get("code_ref") or ""),
                    to_owner=str(args.get("to_owner") or ""),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                )
                return {"code_ref": conf.external_id, "status": conf.status}
            if tool == "marking.withdraw" or op == "marking_withdraw":
                svc.critical_action(
                    tenant_id=tenant,
                    action="marking.withdraw",
                    capabilities=caps,
                    hitl_approved=bool(args.get("hitl_approved")),
                )
                conf = svc.marking.withdraw(
                    tenant_id=tenant,
                    code_ref=str(args.get("code_ref") or ""),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                )
                return {"code_ref": conf.external_id, "status": conf.status}
        except CapabilityDeniedError as exc:
            raise ToolAuthFailedError("tool_permission_denied") from exc
        except CommerceError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolUnavailableError()
