"""Production integration webhook routes."""

from __future__ import annotations

from typing import Annotated, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from integrations.production.adapters.telegram import normalize_telegram_update, verify_telegram_webhook
from integrations.production.errors import ProductionProviderError


def configure_production_integration_router(
    *,
    b2b_service=None,
    billing_service=None,
    stripe_provider=None,
    webhook_secret: str = "",
    telegram_secret: str = "",
) -> APIRouter:
    router = APIRouter(prefix="/integrations", tags=["production-integrations"])

    def _b2b():
        if b2b_service is None:
            raise HTTPException(status_code=503, detail="b2b_unavailable")
        return b2b_service

    def _billing():
        if billing_service is None:
            raise HTTPException(status_code=503, detail="billing_unavailable")
        return billing_service

    @router.post("/telegram/webhook/{tenant_id}")
    async def telegram_webhook(
        tenant_id: str,
        request: Request,
        response: Response,
        x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
        svc=Depends(_b2b),
    ):
        raw = await request.body()
        try:
            payload = verify_telegram_webhook(
                secret_token=telegram_secret,
                header_token=x_telegram_bot_api_secret_token or "",
                raw_body=raw,
            )
        except ProductionProviderError:
            raise HTTPException(status_code=401, detail="webhook_verification_failed")
        normalized = normalize_telegram_update(payload)
        from b2b_commerce.capabilities import CAP_B2B_ASSISTANT_USE, CAP_TELEGRAM_READ

        try:
            result = svc.process_telegram_update(
                tenant_id=tenant_id,
                raw_update=normalized,
                capabilities=(CAP_TELEGRAM_READ, CAP_B2B_ASSISTANT_USE),
            )
            return result
        except Exception as exc:
            from b2b_commerce.errors import B2B_TELEGRAM_DUPLICATE_UPDATE, B2BCommerceError

            if isinstance(exc, B2BCommerceError) and exc.code == B2B_TELEGRAM_DUPLICATE_UPDATE:
                return {"status": "duplicate", "update_id": normalized.get("update_id")}
            raise HTTPException(status_code=400, detail="telegram_processing_failed")

    @router.post("/billing/stripe/webhook")
    async def stripe_webhook(
        request: Request,
        stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
        billing=Depends(_billing),
    ):
        if stripe_provider is None:
            raise HTTPException(status_code=503, detail="stripe_unavailable")
        raw = await request.body()
        try:
            payload = stripe_provider.verify_stripe_signature(raw, stripe_signature or "")
            event = stripe_provider.ingest_stripe_event(payload)
            return billing.process_webhook(event_id=event.event_id, signature=event.signature, payload_hash=event.payload_hash)
        except ProductionProviderError:
            raise HTTPException(status_code=401, detail="webhook_verification_failed")

    return router
