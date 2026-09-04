"""FastAPI webhook router for Telegram interface."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from integrations.production.adapters.telegram import verify_telegram_webhook
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from security.api_auth import get_security_context
from security.identity import RequestSecurityContext
from security.rbac import PERM_OPS_WRITE, RBACDenied, RBACPolicy
from telegram_interface.errors import (
    TGI_ACCESS_DENIED,
    TGI_DUPLICATE_UPDATE,
    TGI_INVALID_UPDATE,
    TGI_PAYLOAD_TOO_LARGE,
    TGI_UNAUTHORIZED,
    TelegramInterfaceError,
)
from telegram_interface.normalize import MAX_TELEGRAM_PAYLOAD_BYTES
from telegram_interface.service import TelegramInterfaceService

_router = APIRouter(prefix="/api/v1/telegram", tags=["telegram-interface"])
_service: TelegramInterfaceService | None = None
_webhook_secret: str = ""
_rbac = RBACPolicy()


def configure_telegram_interface_router(
    service: TelegramInterfaceService | None, *, webhook_secret: str = ""
) -> APIRouter:
    global _service, _webhook_secret
    _service = service
    _webhook_secret = webhook_secret or ""
    return _router


def _svc() -> TelegramInterfaceService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "tgi_unavailable"})
    return _service


def _require_binding_admin(ctx: RequestSecurityContext) -> RequestSecurityContext:
    try:
        _rbac.require(ctx.roles, PERM_OPS_WRITE)
    except RBACDenied as exc:
        raise HTTPException(status_code=403, detail={"code": TGI_UNAUTHORIZED}) from exc
    return ctx


class BindingUpsertRequest(BaseModel):
    tenant_id: str
    owner_id: str
    telegram_user_id: str = Field(..., min_length=1)
    chat_id: str = Field(..., min_length=1)


class BindingStatusRequest(BaseModel):
    tenant_id: str
    telegram_user_id: str = Field(..., min_length=1)
    chat_id: str = Field(..., min_length=1)
    status: str = Field(..., pattern="^(active|revoked|disabled)$")


@_router.post("/webhook/{tenant_id}")
async def telegram_webhook(
    tenant_id: str,
    request: Request,
    response: Response,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
):
    response.headers["Cache-Control"] = "no-store, private"
    raw = await request.body()
    if len(raw) > MAX_TELEGRAM_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail={"code": TGI_PAYLOAD_TOO_LARGE})
    if _webhook_secret:
        try:
            payload = verify_telegram_webhook(
                secret_token=_webhook_secret,
                header_token=x_telegram_bot_api_secret_token or "",
                raw_body=raw,
                max_bytes=MAX_TELEGRAM_PAYLOAD_BYTES,
            )
        except ProductionProviderError as exc:
            if exc.category == ProviderErrorCategory.BAD_REQUEST:
                code = TGI_PAYLOAD_TOO_LARGE if exc.message == "payload_too_large" else TGI_INVALID_UPDATE
                status = 413 if code == TGI_PAYLOAD_TOO_LARGE else 400
                raise HTTPException(status_code=status, detail={"code": code})
            raise HTTPException(status_code=401, detail="webhook_verification_failed")
    else:
        import json

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail={"code": TGI_INVALID_UPDATE})
    try:
        return _svc().handle_payload(tenant_id=tenant_id, payload=payload)
    except TelegramInterfaceError as exc:
        if exc.code == TGI_DUPLICATE_UPDATE:
            return {"status": "duplicate"}
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


@_router.post("/fixture/updates/{tenant_id}")
async def telegram_fixture_update(tenant_id: str, payload: dict[str, Any], response: Response):
    """Test-only direct update ingest — disabled in production via env gate."""
    import os

    response.headers["Cache-Control"] = "no-store, private"
    prod = str(os.environ.get("PANDA_ENV") or os.environ.get("ENVIRONMENT") or "").strip().lower() in {
        "production",
        "prod",
    }
    if prod:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        return _svc().handle_payload(tenant_id=tenant_id, payload=payload)
    except TelegramInterfaceError as exc:
        if exc.code == TGI_DUPLICATE_UPDATE:
            return {"status": "duplicate"}
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


def _tenant_guard(ctx: RequestSecurityContext, tenant_id: str) -> str:
    if tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=403, detail={"code": TGI_ACCESS_DENIED})
    return tenant_id


@_router.post("/admin/bindings")
async def admin_upsert_binding(
    body: BindingUpsertRequest,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    response.headers["Cache-Control"] = "no-store, private"
    _require_binding_admin(ctx)
    tenant = _tenant_guard(ctx, body.tenant_id)
    try:
        return _svc().admin_upsert_binding(
            tenant_id=tenant,
            owner_id=body.owner_id,
            telegram_user_id=body.telegram_user_id,
            chat_id=body.chat_id,
            actor_id=ctx.user_id,
        )
    except TelegramInterfaceError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


@_router.get("/admin/bindings")
async def admin_get_binding(
    tenant_id: str,
    telegram_user_id: str,
    chat_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    response.headers["Cache-Control"] = "no-store, private"
    _require_binding_admin(ctx)
    tenant = _tenant_guard(ctx, tenant_id)
    try:
        return _svc().get_binding_view(
            tenant_id=tenant, telegram_user_id=telegram_user_id, chat_id=chat_id
        )
    except TelegramInterfaceError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


@_router.post("/admin/bindings/status")
async def admin_set_binding_status(
    body: BindingStatusRequest,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    response.headers["Cache-Control"] = "no-store, private"
    _require_binding_admin(ctx)
    tenant = _tenant_guard(ctx, body.tenant_id)
    try:
        return _svc().admin_set_binding_status(
            tenant_id=tenant,
            telegram_user_id=body.telegram_user_id,
            chat_id=body.chat_id,
            status=body.status,
            actor_id=ctx.user_id,
        )
    except TelegramInterfaceError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})
