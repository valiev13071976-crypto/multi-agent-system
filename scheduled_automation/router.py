"""FastAPI router — /api/v1/automations/schedules."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from scheduled_automation.access import ScheduleAccessPolicy
from scheduled_automation.errors import ScheduledAutomationError
from scheduled_automation.service import ScheduledAutomationService
from security.api_auth import get_security_context
from security.identity import RequestSecurityContext

API_VERSION = "v1"
_router = APIRouter(prefix=f"/api/{API_VERSION}/automations", tags=["scheduled-automation"])
_service: ScheduledAutomationService | None = None


def configure_scheduled_automation_router(service: ScheduledAutomationService, policy: ScheduleAccessPolicy | None = None) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc() -> ScheduledAutomationService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "scheduled_automation_unavailable"})
    return _service


def _err(exc: ScheduledAutomationError) -> HTTPException:
    code = 404 if exc.code.endswith("NOT_FOUND") else 403 if exc.code in {"FORBIDDEN", "TENANT_SCOPE_VIOLATION", "CAPABILITY_DENIED"} else 409 if exc.code == "STALE_VERSION" else 400
    return HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message})


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


class CreateScheduleBody(BaseModel):
    tenant_id: str
    name: str
    schedule_type: str
    timezone: str = "UTC"
    start_at: str
    end_at: str | None = None
    interval_seconds: int | None = None
    daily_time: str | None = None
    weekly_day: int | None = None
    max_occurrences: int | None = None
    misfire_policy: str = "SKIP"
    overlap_policy: str = "FORBID"
    workload_class: str = "BACKGROUND"
    priority: int = 3
    target_type: str
    target_payload: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class PatchScheduleBody(BaseModel):
    expected_version: int
    patch: dict[str, Any] = Field(default_factory=dict)


@_router.post("/schedules")
def create_schedule(response: Response, body: CreateScheduleBody, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _no_cache(response)
    try:
        return _svc().create_schedule(ctx, body.model_dump())
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.get("/schedules")
def list_schedules(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    _no_cache(response)
    try:
        return _svc().list_schedules(ctx, tenant_id=tenant_id, limit=limit, offset=offset)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.get("/schedules/{schedule_id}")
def get_schedule(
    response: Response,
    schedule_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
):
    _no_cache(response)
    try:
        return _svc().get_schedule(ctx, tenant_id=tenant_id, schedule_id=schedule_id)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.patch("/schedules/{schedule_id}")
def patch_schedule(
    response: Response,
    schedule_id: str,
    body: PatchScheduleBody,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
):
    _no_cache(response)
    try:
        return _svc().update_schedule(ctx, tenant_id=tenant_id, schedule_id=schedule_id, patch=body.patch, expected_version=body.expected_version)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/schedules/{schedule_id}/enable")
def enable_schedule(response: Response, schedule_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().set_enabled(ctx, tenant_id=tenant_id, schedule_id=schedule_id, enabled=True)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/schedules/{schedule_id}/disable")
def disable_schedule(response: Response, schedule_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().set_enabled(ctx, tenant_id=tenant_id, schedule_id=schedule_id, enabled=False)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/schedules/{schedule_id}/pause")
def pause_schedule(response: Response, schedule_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().pause(ctx, tenant_id=tenant_id, schedule_id=schedule_id)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/schedules/{schedule_id}/resume")
def resume_schedule(response: Response, schedule_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().resume(ctx, tenant_id=tenant_id, schedule_id=schedule_id)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/schedules/{schedule_id}/run-now")
def run_now(response: Response, schedule_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().run_now(ctx, tenant_id=tenant_id, schedule_id=schedule_id)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.get("/schedules/{schedule_id}/runs")
def list_runs(
    response: Response,
    schedule_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
):
    _no_cache(response)
    try:
        return _svc().list_runs(ctx, tenant_id=tenant_id, schedule_id=schedule_id, limit=limit)
    except ScheduledAutomationError as exc:
        raise _err(exc) from exc


@_router.get("/status")
def status():
    from scheduled_automation.config import (
        scheduled_business_automation_engineering_ready,
        scheduled_business_automation_live_active,
        scheduled_business_automation_live_verified,
    )

    return {
        "engineering_ready": scheduled_business_automation_engineering_ready(),
        "live_active": scheduled_business_automation_live_active(),
        "live_verified": scheduled_business_automation_live_verified(),
        "mode": "FIXTURE",
    }
