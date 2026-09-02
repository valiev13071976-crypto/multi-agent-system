"""FastAPI router — /api/v1/scale/optimization."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from scale_optimization.access import ScaleOptimizationAccessPolicy
from scale_optimization.errors import ScaleOptimizationError
from scale_optimization.service import ScaleOptimizationService
from security.api_auth import get_security_context
from security.identity import RequestSecurityContext

API_VERSION = "v1"
_router = APIRouter(prefix=f"/api/{API_VERSION}/scale/optimization", tags=["scale-optimization"])
_service: ScaleOptimizationService | None = None


def configure_scale_optimization_router(
    service: ScaleOptimizationService,
    policy: ScaleOptimizationAccessPolicy | None = None,
) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc() -> ScaleOptimizationService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "scale_optimization_unavailable"})
    return _service


def _err(exc: ScaleOptimizationError) -> HTTPException:
    code = 403 if exc.code in {"FORBIDDEN", "TENANT_SCOPE_VIOLATION"} else 400
    return HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message})


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


class AnalyzeBody(BaseModel):
    signals: dict[str, Any] = Field(default_factory=dict)
    workload_class: str = "INTERACTIVE"


class EvidenceBody(BaseModel):
    path: str
    before: dict[str, Any]
    after: dict[str, Any]
    change: str
    workload_profile: str
    correctness_ok: bool = True


@_router.get("/status")
def status():
    return _svc().status()


@_router.get("/view")
def management_view(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str | None = Query(default=None),
):
    _no_cache(response)
    try:
        return _svc().management_view(ctx, tenant_id=tenant_id)
    except ScaleOptimizationError as exc:
        raise _err(exc) from exc


@_router.post("/analyze")
def analyze(response: Response, body: AnalyzeBody, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _no_cache(response)
    try:
        return _svc().analyze(ctx, signals=body.signals, workload_class=body.workload_class)
    except ScaleOptimizationError as exc:
        raise _err(exc) from exc


@_router.post("/benchmark")
def benchmark(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    profile: str | None = Query(default=None),
):
    _no_cache(response)
    try:
        return _svc().run_benchmark(ctx, profile=profile)
    except ScaleOptimizationError as exc:
        raise _err(exc) from exc


@_router.post("/evidence")
def evidence(response: Response, body: EvidenceBody, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _no_cache(response)
    try:
        return _svc().record_optimization_evidence(
            ctx,
            path=body.path,
            before=body.before,
            after=body.after,
            change=body.change,
            workload_profile=body.workload_profile,
            correctness_ok=body.correctness_ok,
        )
    except ScaleOptimizationError as exc:
        raise _err(exc) from exc
