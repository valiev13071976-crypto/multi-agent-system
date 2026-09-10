"""FastAPI surface for Market Intelligence.

Mirrors the conventions the Telegram Bot API router already uses: an
injected service, RBAC on every route, a hard tenant guard, and
``Cache-Control: no-store`` on every response.

There is no webhook here. A user account is read by polling on demand,
not pushed to, so every route is owner-initiated.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from security.api_auth import get_security_context
from security.identity import RequestSecurityContext
from security.rbac import PERM_OPS_READ, PERM_OPS_WRITE, RBACDenied, RBACPolicy

from market_intel.errors import MI_ACCESS_DENIED, MarketIntelError
from market_intel.service import MarketIntelligenceService

_router = APIRouter(prefix="/api/v1/market-intel", tags=["market-intelligence"])
_service: MarketIntelligenceService | None = None
_rbac = RBACPolicy()


def configure_market_intel_router(service: MarketIntelligenceService | None) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc() -> MarketIntelligenceService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "mi_unavailable"})
    return _service


def _require(ctx: RequestSecurityContext, permission: str) -> RequestSecurityContext:
    try:
        _rbac.require(ctx.roles, permission)
    except RBACDenied as exc:
        raise HTTPException(status_code=403, detail={"code": MI_ACCESS_DENIED}) from exc
    return ctx


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def _fail(exc: MarketIntelError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


class MonitoringRequest(BaseModel):
    channel_id: str = Field(..., min_length=1)
    monitor_state: str = Field(..., pattern="^(pending|enabled|disabled)$")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(default=50, ge=1, le=200)


class SessionRequest(BaseModel):
    session_string: str = Field(..., min_length=1)


@_router.post("/channels/discover")
async def discover_channels(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    limit: int = 200,
):
    _no_store(response)
    _require(ctx, PERM_OPS_WRITE)
    try:
        return _svc().discover_channels(
            tenant_id=ctx.tenant_id, owner_id=ctx.user_id, actor_id=ctx.user_id, limit=limit
        )
    except MarketIntelError as exc:
        raise _fail(exc) from exc


@_router.get("/channels")
async def list_channels(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    monitor_state: str = "",
):
    _no_store(response)
    _require(ctx, PERM_OPS_READ)
    return {"channels": _svc().list_channels(tenant_id=ctx.tenant_id, monitor_state=monitor_state)}


@_router.post("/channels/monitoring")
async def set_monitoring(
    body: MonitoringRequest,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_store(response)
    _require(ctx, PERM_OPS_WRITE)
    try:
        return _svc().set_monitoring(
            tenant_id=ctx.tenant_id,
            channel_id=body.channel_id,
            monitor_state=body.monitor_state,
            actor_id=ctx.user_id,
        )
    except MarketIntelError as exc:
        raise _fail(exc) from exc


@_router.post("/channels/{channel_id}/ingest")
async def ingest_channel(
    channel_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    limit: int = 100,
):
    _no_store(response)
    _require(ctx, PERM_OPS_WRITE)
    try:
        return _svc().ingest_channel(tenant_id=ctx.tenant_id, channel_id=channel_id, limit=limit)
    except MarketIntelError as exc:
        raise _fail(exc) from exc


@_router.post("/channels/{channel_id}/search")
async def search_channel(
    channel_id: str,
    body: SearchRequest,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_store(response)
    _require(ctx, PERM_OPS_WRITE)
    try:
        return _svc().search_channel(
            tenant_id=ctx.tenant_id, channel_id=channel_id, query=body.query, limit=body.limit
        )
    except MarketIntelError as exc:
        raise _fail(exc) from exc


@_router.get("/observations")
async def list_observations(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    product_id: str = "",
    channel_id: str = "",
):
    _no_store(response)
    _require(ctx, PERM_OPS_READ)
    return {
        "observations": _svc().list_observations(
            tenant_id=ctx.tenant_id, product_id=product_id, channel_id=channel_id
        )
    }


@_router.get("/price-comparison/{product_id}")
async def price_comparison(
    product_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_store(response)
    _require(ctx, PERM_OPS_READ)
    try:
        comparison = _svc().price_comparison(tenant_id=ctx.tenant_id, product_id=product_id)
    except MarketIntelError as exc:
        raise _fail(exc) from exc
    return {
        "product_id": comparison.product_id,
        "currency": comparison.currency,
        "observation_count": comparison.observation_count,
        "our_selling_price": _money(comparison.our_selling_price),
        "our_purchase_price": _money(comparison.our_purchase_price),
        "min_price": _money(comparison.min_price),
        "max_price": _money(comparison.max_price),
        "median_price": _money(comparison.median_price),
        "cheapest_observation_id": comparison.cheapest_observation_id,
        "sources": list(comparison.sources),
        "excluded": list(comparison.excluded),
    }


@_router.post("/session")
async def store_session(
    body: SessionRequest,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    """Store the owner's MTProto session. The value is encrypted at rest
    and is never returned by any route."""
    _no_store(response)
    _require(ctx, PERM_OPS_WRITE)
    vault = getattr(_svc(), "session_vault", None)
    if vault is None:
        raise HTTPException(status_code=503, detail={"code": "mi_unavailable"})
    try:
        return vault.store_session(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            session_string=body.session_string,
            actor_id=ctx.user_id,
        )
    except MarketIntelError as exc:
        raise _fail(exc) from exc


@_router.delete("/session")
async def revoke_session(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_store(response)
    _require(ctx, PERM_OPS_WRITE)
    vault = getattr(_svc(), "session_vault", None)
    if vault is None:
        raise HTTPException(status_code=503, detail={"code": "mi_unavailable"})
    return vault.revoke(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, actor_id=ctx.user_id)


def _money(value) -> str:
    return "" if value is None else str(value)
