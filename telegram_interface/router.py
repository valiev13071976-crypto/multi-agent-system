"""FastAPI webhook router for Telegram interface."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, Response

from integrations.production.adapters.telegram import verify_telegram_webhook
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from telegram_interface.errors import (
    TGI_DUPLICATE_UPDATE,
    TGI_INVALID_UPDATE,
    TGI_PAYLOAD_TOO_LARGE,
    TelegramInterfaceError,
)
from telegram_interface.normalize import MAX_TELEGRAM_PAYLOAD_BYTES
from telegram_interface.service import TelegramInterfaceService

_router = APIRouter(prefix="/api/v1/telegram", tags=["telegram-interface"])
_service: TelegramInterfaceService | None = None
_webhook_secret: str = ""


def configure_telegram_interface_router(
    service: TelegramInterfaceService, *, webhook_secret: str = ""
) -> APIRouter:
    global _service, _webhook_secret
    _service = service
    _webhook_secret = webhook_secret or ""
    return _router


def _svc() -> TelegramInterfaceService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "tgi_unavailable"})
    return _service


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
