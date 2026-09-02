"""FastAPI router — /api/v1/analytics."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from analytics_dashboard.access import AnalyticsAccessPolicy
from analytics_dashboard.errors import AnalyticsError
from analytics_dashboard.models import AnalyticsQuery
from analytics_dashboard.service import AnalyticsDashboardService
from security.api_auth import get_security_context
from security.identity import RequestSecurityContext

API_VERSION = "v1"
_router = APIRouter(prefix=f"/api/{API_VERSION}/analytics", tags=["analytics-dashboard"])
_service: AnalyticsDashboardService | None = None
_policy: AnalyticsAccessPolicy | None = None


def configure_analytics_dashboard_router(
    service: AnalyticsDashboardService, policy: AnalyticsAccessPolicy | None = None
) -> APIRouter:
    global _service, _policy
    _service = service
    _policy = policy or AnalyticsAccessPolicy()
    return _router


def _svc() -> AnalyticsDashboardService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "analytics_unavailable"})
    return _service


def _err(exc: AnalyticsError) -> HTTPException:
    code = 403 if exc.code in {"FORBIDDEN", "TENANT_SCOPE_VIOLATION"} else 400
    return HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message})


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


class MetricsQueryBody(BaseModel):
    tenant_id: str
    metrics: list[str] = Field(..., min_length=1, max_length=20)
    start: str
    end: str
    timezone: str = "Europe/Moscow"
    granularity: str = "day"
    filters: dict[str, Any] = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


@_router.get("/overview")
def get_overview(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
    window: str = Query(default="7d"),
):
    _no_cache(response)
    try:
        return _svc().overview(ctx, tenant_id=tenant_id, window=window)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.post("/metrics")
def post_metrics(
    response: Response,
    body: MetricsQueryBody,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    q = AnalyticsQuery(
        tenant_id=body.tenant_id,
        metrics=body.metrics,
        start=body.start,
        end=body.end,
        timezone=body.timezone,
        granularity=body.granularity,
        filters=body.filters,
        group_by=body.group_by,
        limit=body.limit,
        offset=body.offset,
    )
    try:
        return _svc().query_metrics(ctx, q)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.get("/timeseries")
def get_timeseries(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
    metric_id: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    granularity: str = Query(default="day"),
):
    _no_cache(response)
    try:
        return _svc().timeseries(ctx, tenant_id=tenant_id, metric_id=metric_id, start=start, end=end, granularity=granularity)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.get("/marketplaces")
def get_marketplaces(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
    window: str = Query(default="30d"),
):
    _no_cache(response)
    try:
        return _svc().marketplaces(ctx, tenant_id=tenant_id, window=window)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.get("/products")
def get_products(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    _no_cache(response)
    try:
        return _svc().products(ctx, tenant_id=tenant_id, limit=limit, offset=offset)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.get("/integrations")
def get_integrations(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
):
    _no_cache(response)
    try:
        return _svc().integrations_health(ctx, tenant_id=tenant_id)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.get("/workflows")
def get_workflows(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
):
    _no_cache(response)
    try:
        return _svc().workflows(ctx, tenant_id=tenant_id)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.get("/finops")
def get_finops(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
):
    _no_cache(response)
    try:
        return _svc().finops(ctx, tenant_id=tenant_id)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.get("/alerts")
def get_alerts(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
):
    _no_cache(response)
    try:
        return _svc().alerts(ctx, tenant_id=tenant_id)
    except AnalyticsError as exc:
        raise _err(exc) from exc


@_router.get("/status")
def get_status():
    from analytics_dashboard.config import (
        analytics_dashboard_engineering_ready,
        analytics_dashboard_live_active,
        analytics_dashboard_live_verified,
    )

    return {
        "engineering_ready": analytics_dashboard_engineering_ready(),
        "live_active": analytics_dashboard_live_active(),
        "live_verified": analytics_dashboard_live_verified(),
        "mode": "FIXTURE",
    }
