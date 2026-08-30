"""Tool platform adapter for B2B / Telegram Commerce (Block 13)."""

from __future__ import annotations

from b2b_commerce.capabilities import (
    CAP_B2B_ASSISTANT_USE,
    CAP_B2B_CUSTOMER_WRITE,
    CAP_B2B_ORDER_DRAFT,
    CAP_B2B_ORDER_SUBMIT,
    CAP_B2B_QUOTE_CREATE,
    CAP_B2B_QUOTE_READ,
    CAP_B2B_QUOTE_SEND,
    CAP_B2B_READ,
    CAP_B2B_SUPPLIER_READ,
    CAP_B2B_SUPPLIER_WRITE,
    CAP_B2B_WHOLESALE_COMPARE,
    CAP_B2B_WHOLESALE_INGEST,
    CAP_B2B_WHOLESALE_READ,
    CAP_TELEGRAM_READ,
    CAP_TELEGRAM_SEND,
)
from b2b_commerce.errors import B2BBatchRequired, B2BCommerceError
from b2b_commerce.service import B2BCommerceService
from tools.errors import ToolArgumentInvalidError, ToolNotFoundError, ToolPermanentFailureError, ToolUnavailableError


class B2BCommerceToolAdapter:
    adapter_id = "b2b_commerce"

    def __init__(self, service: B2BCommerceService | None = None, *, enabled: bool = False):
        self._service = service
        self._enabled = enabled and service is not None

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("b2b.") or tool_id.startswith("telegram.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._enabled else ADAPTER_UNAVAILABLE

    def _caps(self, request) -> tuple[str, ...]:
        return tuple(getattr(request, "requested_capabilities", None) or ())

    def _tenant(self, request) -> str:
        return str(request.tenant_id or "legacy-default")

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        tenant = self._tenant(request)
        args = dict(request.arguments or {})
        op = request.operation
        svc = self._service
        caps = self._caps(request)
        try:
            if op in {"supplier.get", "get_supplier"}:
                supplier = svc.get_supplier(tenant_id=tenant, supplier_id=str(args["supplier_id"]), capabilities=caps or (CAP_B2B_SUPPLIER_READ,))
                return {"supplier": supplier.__dict__ if supplier else None}
            if op in {"wholesale.list", "list_wholesale"}:
                return svc.list_wholesale(tenant_id=tenant, supplier_id=str(args.get("supplier_id") or ""), capabilities=caps or (CAP_B2B_WHOLESALE_READ,))
            if op in {"wholesale.compare", "compare"}:
                return svc.compare_wholesale(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    requested_quantity=int(args["requested_quantity"]),
                    preferred_supplier=str(args.get("preferred_supplier") or ""),
                    capabilities=caps or (CAP_B2B_WHOLESALE_COMPARE,),
                )
            if op in {"wholesale.changes", "changes"}:
                return svc.wholesale_changes(
                    tenant_id=tenant,
                    supplier_id=str(args["supplier_id"]),
                    old_version_id=str(args["old_version_id"]),
                    new_version_id=str(args["new_version_id"]),
                    capabilities=caps or (CAP_B2B_WHOLESALE_READ,),
                )
            if op in {"quote.get"}:
                return svc.get_quote(
                    tenant_id=tenant,
                    quote_id=str(args["quote_id"]),
                    version_id=str(args["version_id"]),
                    customer_view=bool(args.get("customer_view")),
                    capabilities=caps or (CAP_B2B_QUOTE_READ,),
                )
            if op in {"conversation.get", "get_conversation"}:
                return {"conversation": svc.get_conversation(tenant_id=tenant, conversation_id=str(args["conversation_id"]), capabilities=caps or (CAP_B2B_READ,))}
        except B2BBatchRequired as exc:
            raise ToolArgumentInvalidError(str(exc.code)) from exc
        except B2BCommerceError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolNotFoundError("operation_not_supported")

    async def execute_write(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        tenant = self._tenant(request)
        args = dict(request.arguments or {})
        op = request.operation
        svc = self._service
        caps = self._caps(request)
        try:
            if op in {"supplier.create", "create_supplier"}:
                supplier = svc.create_supplier(
                    tenant_id=tenant,
                    name=str(args["name"]),
                    source_bindings=tuple(args.get("source_bindings") or ()),
                    capabilities=caps or (CAP_B2B_SUPPLIER_WRITE,),
                )
                return {"supplier": supplier.__dict__}
            if op in {"wholesale.ingest", "ingest"}:
                return svc.ingest_wholesale(
                    tenant_id=tenant,
                    supplier_id=str(args["supplier_id"]),
                    rows=list(args.get("rows") or []),
                    file_bytes=args.get("file_bytes"),
                    filename=str(args.get("filename") or ""),
                    source_class=str(args.get("source_class") or "SUPPLIER_FILE"),
                    source_key=str(args.get("source_key") or ""),
                    artifact_id=str(args.get("artifact_id") or ""),
                    bulk=bool(args.get("bulk")),
                    job_id=args.get("job_id"),
                    checkpoint=int(args.get("checkpoint") or 0),
                    capabilities=caps or (CAP_B2B_WHOLESALE_INGEST,),
                )
            if op in {"customer.create", "create_customer"}:
                customer = svc.create_customer(
                    tenant_id=tenant,
                    display_name=str(args["display_name"]),
                    verification_state=str(args.get("verification_state") or "CANDIDATE"),
                    capabilities=caps or (CAP_B2B_CUSTOMER_WRITE,),
                )
                return {"customer": customer.__dict__}
            if op in {"inquiry.create"}:
                return svc.process_telegram_update(
                    tenant_id=tenant,
                    raw_update=dict(args.get("update") or args),
                    capabilities=caps or (CAP_TELEGRAM_READ, CAP_B2B_ASSISTANT_USE),
                )
            if op in {"quote.create", "create_quote"}:
                return svc.create_quote(
                    tenant_id=tenant,
                    conversation_id=str(args["conversation_id"]),
                    inquiry_id=str(args["inquiry_id"]),
                    customer_id=str(args["customer_id"]),
                    items=list(args.get("items") or []),
                    discount_pct=str(args.get("discount_pct") or "0"),
                    capabilities=caps or (CAP_B2B_QUOTE_CREATE,),
                )
            if op in {"quote.send", "send_quote"}:
                prepared = svc.prepare_quote_send(
                    tenant_id=tenant,
                    quote_id=str(args["quote_id"]),
                    version_id=str(args["version_id"]),
                    chat_id=str(args["chat_id"]),
                    capabilities=caps or (CAP_B2B_QUOTE_SEND,),
                )
                if prepared.get("idempotent"):
                    return prepared
                send = svc.send_telegram_message(
                    tenant_id=tenant,
                    chat_id=str(args["chat_id"]),
                    text=str(prepared["text"]),
                    idempotency_key=str(prepared["idempotency_key"]),
                    capabilities=caps or (CAP_TELEGRAM_SEND,),
                )
                receipt = svc.record_quote_sent(
                    tenant_id=tenant,
                    quote_id=str(args["quote_id"]),
                    version_id=str(args["version_id"]),
                    chat_binding_id=str(args.get("chat_binding_id") or args["chat_id"]),
                    idempotency_key=str(prepared["idempotency_key"]),
                    provider_message_id=str(send.get("provider_message_id") or ""),
                )
                return {"sent": True, "receipt": receipt, "external_ref": send.get("provider_message_id")}
            if op in {"order.draft", "create_order_draft"}:
                return svc.create_order_draft(
                    tenant_id=tenant,
                    customer_id=str(args["customer_id"]),
                    conversation_id=str(args["conversation_id"]),
                    quote_id=str(args["quote_id"]),
                    quote_version_id=str(args["quote_version_id"]),
                    capabilities=caps or (CAP_B2B_ORDER_DRAFT,),
                )
            if op in {"order.submit", "submit_order"}:
                return svc.submit_order(
                    tenant_id=tenant,
                    draft_id=str(args["draft_id"]),
                    confirmation_token=str(args["confirmation_token"]),
                    capabilities=caps or (CAP_B2B_ORDER_SUBMIT,),
                )
            if op in {"message.send", "send_message"}:
                return svc.send_telegram_message(
                    tenant_id=tenant,
                    chat_id=str(args["chat_id"]),
                    text=str(args["text"]),
                    idempotency_key=str(args.get("idempotency_key") or args.get("idempotency") or ""),
                    capabilities=caps or (CAP_TELEGRAM_SEND,),
                )
            if op in {"handoff.create"}:
                return svc.create_handoff(
                    tenant_id=tenant,
                    conversation_id=str(args["conversation_id"]),
                    reason=str(args.get("reason") or "policy"),
                    context=dict(args.get("context") or {}),
                    capabilities=caps,
                )
            if op in {"assistant.use", "assistant_process"}:
                return svc.assistant_process(
                    tenant_id=tenant,
                    conversation_id=str(args["conversation_id"]),
                    text=str(args.get("text") or ""),
                    resolved_items=list(args.get("resolved_items") or []),
                    data_scope=str(args.get("data_scope") or "CUSTOMER"),
                    capabilities=caps or (CAP_B2B_ASSISTANT_USE,),
                )
            if op in {"telegram.register"}:
                binding = svc.register_telegram_account(
                    tenant_id=tenant,
                    bot_id=str(args["bot_id"]),
                    secret_ref=str(args.get("secret_ref") or "telegram/bot-token"),
                    capabilities=caps or (CAP_TELEGRAM_READ,),
                )
                return {"binding": binding.__dict__}
            if op in {"telegram.bind_chat"}:
                binding = svc.bind_telegram_chat(
                    tenant_id=tenant,
                    account_binding_id=str(args["account_binding_id"]),
                    chat_id=str(args["chat_id"]),
                    customer_id=str(args.get("customer_id") or ""),
                    capabilities=caps or (CAP_TELEGRAM_READ,),
                )
                return {"binding": binding.__dict__}
        except B2BBatchRequired as exc:
            raise ToolArgumentInvalidError(str(exc.code)) from exc
        except B2BCommerceError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolNotFoundError("operation_not_supported")
