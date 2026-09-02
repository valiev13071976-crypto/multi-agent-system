"""FastAPI router — /api/v1/automations/controlled."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from controlled_automation.access import ControlledAutomationAccessPolicy
from controlled_automation.errors import ControlledAutomationError
from controlled_automation.service import ControlledAutomationService
from security.api_auth import get_security_context
from security.identity import RequestSecurityContext

API_VERSION = "v1"
_router = APIRouter(prefix=f"/api/{API_VERSION}/automations/controlled", tags=["controlled-automation"])
_service: ControlledAutomationService | None = None


def configure_controlled_automation_router(service: ControlledAutomationService, policy: ControlledAutomationAccessPolicy | None = None) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc() -> ControlledAutomationService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "controlled_automation_unavailable"})
    return _service


def _err(exc: ControlledAutomationError) -> HTTPException:
    code = 404 if exc.code.endswith("NOT_FOUND") else 403 if exc.code in {"FORBIDDEN", "TENANT_SCOPE_VIOLATION", "CAPABILITY_DENIED"} else 409 if exc.code == "STALE_VERSION" else 400
    return HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message})


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


class CreateAutomationBody(BaseModel):
    tenant_id: str
    name: str
    description: str = ""
    trigger: dict[str, Any] = Field(default_factory=dict)
    conditions: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    policy: dict[str, Any] = Field(default_factory=dict)
    risk_class: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PatchAutomationBody(BaseModel):
    expected_version: int
    patch: dict[str, Any] = Field(default_factory=dict)


class RunContextBody(BaseModel):
    facts: dict[str, Any] = Field(default_factory=dict)
    event: dict[str, Any] | None = None


class ApproveBody(BaseModel):
    approval_id: str
    fingerprint: str


@_router.get("/status")
def status():
    from controlled_automation.config import (
        controlled_automation_expansion_engineering_ready,
        controlled_automation_expansion_live_active,
        controlled_automation_expansion_live_verified,
    )

    return {
        "engineering_ready": controlled_automation_expansion_engineering_ready(),
        "live_active": controlled_automation_expansion_live_active(),
        "live_verified": controlled_automation_expansion_live_verified(),
        "mode": "FIXTURE",
    }


@_router.post("")
def create_automation(response: Response, body: CreateAutomationBody, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _no_cache(response)
    try:
        return _svc().create(ctx, body.model_dump())
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.get("")
def list_automations(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    _no_cache(response)
    try:
        return _svc().list(ctx, tenant_id=tenant_id, limit=limit, offset=offset)
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.get("/{automation_id}")
def get_automation(response: Response, automation_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().get(ctx, tenant_id=tenant_id, automation_id=automation_id)
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.patch("/{automation_id}")
def patch_automation(response: Response, automation_id: str, body: PatchAutomationBody, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().update(ctx, tenant_id=tenant_id, automation_id=automation_id, patch=body.patch, expected_version=body.expected_version)
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/{automation_id}/enable")
def enable(response: Response, automation_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().enable(ctx, tenant_id=tenant_id, automation_id=automation_id)
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/{automation_id}/pause")
def pause(response: Response, automation_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().pause(ctx, tenant_id=tenant_id, automation_id=automation_id)
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/{automation_id}/resume")
def resume(response: Response, automation_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().resume(ctx, tenant_id=tenant_id, automation_id=automation_id)
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/{automation_id}/disable")
def disable(response: Response, automation_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().disable(ctx, tenant_id=tenant_id, automation_id=automation_id)
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/{automation_id}/run-now")
def run_now(response: Response, automation_id: str, body: RunContextBody, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().run_now(ctx, tenant_id=tenant_id, automation_id=automation_id, context={"facts": body.facts})
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.post("/{automation_id}/dry-run")
def dry_run(response: Response, automation_id: str, body: RunContextBody, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...)):
    _no_cache(response)
    try:
        return _svc().dry_run(ctx, tenant_id=tenant_id, automation_id=automation_id, context={"facts": body.facts})
    except ControlledAutomationError as exc:
        raise _err(exc) from exc


@_router.get("/{automation_id}/runs")
def list_runs(response: Response, automation_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str = Query(...), limit: int = Query(default=50, ge=1, le=100)):
    _no_cache(response)
    try:
        return _svc().list_runs(ctx, tenant_id=tenant_id, automation_id=automation_id, limit=limit)
    except ControlledAutomationError as exc:
        raise _err(exc) from exc
