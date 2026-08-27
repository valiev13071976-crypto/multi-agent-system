"""Payments tool adapters — ToolGateway only."""

from __future__ import annotations

from payments.capabilities import (
    CAP_BANK_STATEMENT_READ,
    CAP_BANK_TRANSACTION_READ,
    CAP_PAYMENTS_ALLOCATE,
    CAP_PAYMENTS_EXECUTE_REFUND,
    CAP_PAYMENTS_PREPARE_REFUND,
    CAP_PAYMENTS_READ,
    CAP_PAYMENTS_RECONCILE,
    LLM_DEFAULT_DENY,
)
from payments.errors import CapabilityDeniedError, PaymentsError, TenantAccessDeniedError
from tools.errors import ToolAuthFailedError, ToolPermanentFailureError, ToolUnavailableError
from tools.models import ADAPTER_DEGRADED, ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE


class PaymentsToolAdapter:
    adapter_id = "payments"

    def __init__(self, service=None, *, enabled: bool = False):
        self._service = service
        self._enabled = enabled and service is not None

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("payments.") or tool_id.startswith("bank.")

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
        tool = request.tool_id
        try:
            if tool in {"payments.read", "payments.status"}:
                payment_id = str(args.get("payment_id") or "")
                if payment_id:
                    row = svc.store.get_payment(tenant, payment_id)
                    return row or {}
                return {"payments": svc.store.list_payments(tenant)[:50]}
            if tool == "payments.match":
                if args.get("payment_id"):
                    m = svc.match_payment(tenant, str(args["payment_id"]))
                else:
                    m = svc.match_bank_tx(tenant, str(args.get("transaction_id") or ""))
                return {
                    "match_id": m.match_id,
                    "status": m.status,
                    "confidence": m.confidence,
                    "review_required": m.review_required,
                    "selected_order_id": m.selected_order_id,
                }
            if tool == "payments.reconcile":
                return svc.reconcile_tenant(tenant)
            if tool == "bank.transactions":
                return {"transactions": svc.store.list_bank_tx(tenant)[:100]}
            if tool == "bank.statement.read":
                return {
                    "account_ref": args.get("account_ref"),
                    "transactions": [
                        t
                        for t in svc.store.list_bank_tx(tenant)
                        if not args.get("account_ref")
                        or t.get("account_ref") == args.get("account_ref")
                    ][:100],
                }
        except TenantAccessDeniedError as exc:
            raise ToolAuthFailedError("integration_not_available") from exc
        except PaymentsError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolUnavailableError()

    async def execute_write(self, request, context) -> dict:
        if not self._enabled or self._service is None:
            raise ToolUnavailableError()
        caps = self._caps(request)
        args = dict(request.arguments or {})
        tenant = request.tenant_id
        tool = request.tool_id
        svc = self._service
        try:
            if tool == "payments.allocate":
                if CAP_PAYMENTS_ALLOCATE not in caps:
                    raise CapabilityDeniedError("capability_denied")
                alloc = svc.allocate(
                    tenant,
                    str(args.get("payment_id") or ""),
                    str(args.get("order_id") or ""),
                    float(args.get("amount") or 0),
                    invoice_id=str(args.get("invoice_id") or ""),
                    capabilities=caps,
                    idempotency_key=str(args.get("idempotency_key") or ""),
                )
                return {"allocation_id": alloc.allocation_id, "status": alloc.status}
            if tool == "payments.prepare_refund":
                if CAP_PAYMENTS_PREPARE_REFUND not in caps:
                    raise CapabilityDeniedError("capability_denied")
                ref = svc.prepare_refund(
                    tenant,
                    payment_id=str(args.get("payment_id") or ""),
                    amount=float(args.get("amount") or 0),
                    reason=str(args.get("reason") or ""),
                    order_id=str(args.get("order_id") or ""),
                    capabilities=caps,
                )
                return {"refund_id": ref.refund_id, "status": ref.status}
            if tool == "payments.execute_refund":
                if CAP_PAYMENTS_EXECUTE_REFUND not in caps:
                    raise CapabilityDeniedError("capability_denied")
                if CAP_PAYMENTS_EXECUTE_REFUND in LLM_DEFAULT_DENY and not args.get(
                    "approval_id"
                ):
                    raise CapabilityDeniedError("capability_denied")
                ref = svc.execute_refund(
                    tenant,
                    refund_id=str(args.get("refund_id") or ""),
                    capabilities=caps,
                    approval_id=str(args.get("approval_id") or ""),
                    approved_by=str(args.get("approved_by") or ""),
                    idempotency_key=str(args.get("idempotency_key") or ""),
                )
                return {"refund_id": ref.refund_id, "status": ref.status}
        except CapabilityDeniedError as exc:
            raise ToolAuthFailedError(exc.code) from exc
        except TenantAccessDeniedError as exc:
            raise ToolAuthFailedError("integration_not_available") from exc
        except PaymentsError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolUnavailableError()
