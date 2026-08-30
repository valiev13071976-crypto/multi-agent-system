"""FastAPI router for SaaS product API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from saas_product.context import resolve_product_context
from saas_product.deployment import validate_production_config
from saas_product.errors import SaaSError
from saas_product.service import SaaSProductService
from security.api_auth import get_security_context
from security.identity import RequestSecurityContext

_router = APIRouter(prefix="/api/product", tags=["product"])
_service: SaaSProductService | None = None


def configure_saas_product_router(service: SaaSProductService) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc() -> SaaSProductService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"error": "product_unavailable"})
    return _service


def _err(exc: SaaSError) -> HTTPException:
    code = 404 if "not_found" in exc.code else 409 if exc.code.endswith("conflict") or exc.code == "saas_stale_state" else 403
    if exc.code == "saas_quota_exceeded" or exc.code == "saas_entitlement_denied":
        code = 402
    return HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message})


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class InviteRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    role: str = "MEMBER"


class AcceptInviteRequest(BaseModel):
    token: str = Field(..., min_length=16, max_length=256)


class RoleChangeRequest(BaseModel):
    role: str
    expected_version: int = Field(..., ge=1)


class CheckoutRequest(BaseModel):
    plan_id: str
    plan_version: str
    idempotency_key: str = Field(..., min_length=8, max_length=128)


class WebhookRequest(BaseModel):
    event_id: str
    signature: str
    payload_hash: str


class SwitchTenantRequest(BaseModel):
    tenant_id: str


class ConfirmDeletionRequest(BaseModel):
    confirmation_token: str = Field(..., min_length=8, max_length=128)


def _ctx(
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    x_active_tenant: str | None = Header(default=None, alias="X-Active-Tenant"),
) -> RequestSecurityContext:
    try:
        return resolve_product_context(ctx, _svc(), active_tenant=x_active_tenant)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.get("/onboarding")
async def onboarding(response: Response, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    _no_cache(response)
    try:
        return _svc().onboarding(ctx)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/tenants")
async def create_tenant(body: CreateTenantRequest, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().create_tenant(ctx, name=body.name).__dict__
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.get("/tenants")
async def list_tenants(response: Response, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    _no_cache(response)
    return [t.__dict__ for t in _svc().list_my_tenants(ctx)]


@_router.post("/tenant/switch")
async def switch_tenant(body: SwitchTenantRequest, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().switch_tenant(ctx, tenant_id=body.tenant_id)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.get("/members")
async def list_members(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(_ctx)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _no_cache(response)
    try:
        items, total = _svc().list_members(ctx, limit=limit, offset=offset)
        return {"items": [i.__dict__ for i in items], "page": {"total": total, "limit": limit, "offset": offset}}
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/members/invite")
async def invite_member(body: InviteRequest, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        inv, token = _svc().invite_member(ctx, email=body.email, role=body.role)
        return {"invitation": inv.__dict__, "invite_token": token}
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/members/invite/accept")
async def accept_invite(body: AcceptInviteRequest, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().accept_invitation(ctx, token=body.token).__dict__
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/members/invite/{invitation_id}/revoke")
async def revoke_invite(invitation_id: str, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().revoke_invitation(ctx, invitation_id).__dict__
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/members/{membership_id}/role")
async def change_role(membership_id: str, body: RoleChangeRequest, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().change_role(ctx, membership_id, role=body.role, expected_version=body.expected_version).__dict__
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/members/{membership_id}/remove")
async def remove_member(membership_id: str, body: RoleChangeRequest, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().remove_member(ctx, membership_id, expected_version=body.expected_version).__dict__
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.get("/entitlements")
async def entitlements(response: Response, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    _no_cache(response)
    try:
        return _svc().get_entitlements(ctx)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.get("/plans")
async def plans(ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    from saas_product.plans import list_plans

    return [p.__dict__ for p in list_plans()]


@_router.post("/billing/checkout")
async def checkout(body: CheckoutRequest, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().create_checkout(ctx, plan_id=body.plan_id, plan_version=body.plan_version, idempotency_key=body.idempotency_key)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/billing/webhook")
async def billing_webhook(body: WebhookRequest):
    try:
        return _svc().billing.process_webhook(event_id=body.event_id, signature=body.signature, payload_hash=body.payload_hash)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.get("/billing/status")
async def billing_status(response: Response, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    _no_cache(response)
    try:
        return _svc().billing_status(ctx)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/billing/cancel")
async def cancel_subscription(ctx: Annotated[RequestSecurityContext, Depends(_ctx)], at_period_end: bool = True):
    try:
        return _svc().cancel_subscription(ctx, at_period_end=at_period_end)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.get("/privacy/inventory")
async def privacy_inventory(response: Response, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    _no_cache(response)
    try:
        return _svc().privacy_inventory(ctx)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/privacy/export")
async def privacy_export(ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().request_export(ctx)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/privacy/delete/account")
async def delete_account(ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().request_account_deletion(ctx)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/privacy/delete/tenant")
async def delete_tenant(ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().request_tenant_deletion(ctx)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.post("/privacy/delete/account/{job_id}/confirm")
async def confirm_delete(job_id: str, body: ConfirmDeletionRequest, ctx: Annotated[RequestSecurityContext, Depends(_ctx)]):
    try:
        return _svc().confirm_deletion(ctx, job_id, confirmation_token=body.confirmation_token)
    except SaaSError as exc:
        raise _err(exc) from exc


@_router.get("/readiness")
async def commercial_readiness():
    return validate_production_config().as_dict()
