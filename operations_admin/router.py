"""FastAPI router for operations admin API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from operations_admin.access import AdminAuthorizationPolicy
from operations_admin.commands import (
    ActivateRoutingCommand,
    ApprovalDecisionCommand,
    CancelRunCommand,
    RedriveDLQCommand,
    RollbackRoutingCommand,
    confirmation_token,
)
from operations_admin.errors import AdminError
from operations_admin.service import OperationsAdminService
from security.api_auth import get_security_context
from security.identity import RequestSecurityContext

_router = APIRouter(prefix="/api/admin/ops", tags=["admin-ops"])
_service: OperationsAdminService | None = None
_policy: AdminAuthorizationPolicy | None = None


def configure_operations_admin_router(service: OperationsAdminService, policy: AdminAuthorizationPolicy | None = None) -> APIRouter:
    global _service, _policy
    _service = service
    _policy = policy or AdminAuthorizationPolicy()
    return _router


def _svc() -> OperationsAdminService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"error": "admin_unavailable"})
    return _service


def _require_admin(ctx: RequestSecurityContext) -> RequestSecurityContext:
    from operations_admin.capabilities import PERM_OPS_READ

    try:
        (_policy or AdminAuthorizationPolicy()).require(ctx, PERM_OPS_READ)
    except AdminError as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
    return ctx


def _err(exc: AdminError) -> HTTPException:
    code = 404 if "not_found" in exc.code else 409 if exc.code == "admin_stale_state" else 403
    return HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message})


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int


class CancelRunRequest(BaseModel):
    tenant_id: str
    reason: str = ""
    expected_status: str | None = None


class RedriveRequest(BaseModel):
    tenant_id: str
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    reason: str = ""


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


class DrainRequest(BaseModel):
    reason: str = ""


class ResumeRequest(BaseModel):
    reason: str = ""


class ApprovalDecisionRequest(BaseModel):
    tenant_id: str
    decision: str = Field(..., pattern="^(approve|deny)$")
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    reason: str = ""


class ActivateRoutingRequest(BaseModel):
    candidate_id: str
    expected_policy_version: str
    confirmation_token: str
    reason: str = ""


class RollbackRoutingRequest(BaseModel):
    confirmation_token: str
    reason: str = ""


@_router.get("/dashboard")
async def dashboard(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _require_admin(ctx)
    _no_cache(response)
    d = _svc().dashboard(ctx)
    return {
        "generated_at": d.generated_at,
        "health": d.health.__dict__,
        "active_runs": d.active_runs,
        "queued_jobs": d.queued_jobs,
        "failed_jobs": d.failed_jobs,
        "dlq_count": d.dlq_count,
        "pending_approvals": d.pending_approvals,
        "alerts": [a.__dict__ for a in d.alerts],
        "cost_summary": d.cost_summary.__dict__,
    }


@_router.get("/production-foundation")
async def production_foundation(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _require_admin(ctx)
    _no_cache(response)
    return _svc().production_foundation_status(ctx)


@_router.get("/health")
async def health(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return _svc().system_health(ctx).__dict__


@_router.get("/runs")
async def runs(
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    tenant_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _require_admin(ctx)
    items, total = _svc().list_runs(ctx, tenant_id=tenant_id, status=status, limit=limit, offset=offset)
    return {"items": [i.__dict__ for i in items], "page": PageMeta(total=total, limit=limit, offset=offset).model_dump()}


@_router.get("/runs/{workflow_id}")
async def run_detail(workflow_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], tenant_id: str | None = None):
    _require_admin(ctx)
    try:
        return _svc().get_run(ctx, workflow_id, tenant_id=tenant_id).__dict__
    except AdminError as exc:
        raise _err(exc) from exc


@_router.post("/runs/{workflow_id}/cancel")
async def cancel_run(workflow_id: str, body: CancelRunRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return await _svc().cancel_run(ctx, CancelRunCommand(workflow_id=workflow_id, tenant_id=body.tenant_id, reason=body.reason, expected_status=body.expected_status))
    except AdminError as exc:
        raise _err(exc) from exc


@_router.get("/queues")
async def queues(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return [q.__dict__ for q in _svc().list_queues(ctx)]


@_router.get("/workers")
async def workers(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return [w.__dict__ for w in _svc().list_workers(ctx)]


@_router.get("/routing")
async def routing(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return _svc().routing_health(ctx).__dict__


@_router.get("/tools")
async def tools(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return [t.__dict__ for t in _svc().list_tools(ctx)]


@_router.get("/costs")
async def costs(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], window: str = "24h"):
    _require_admin(ctx)
    return _svc().usage_summary(ctx, window=window).__dict__


@_router.get("/budgets/{tenant_id}")
async def budget(tenant_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().budget_status(ctx, tenant_id=tenant_id).__dict__
    except AdminError as exc:
        raise _err(exc) from exc


@_router.get("/dlq")
async def dlq(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
    _require_admin(ctx)
    items, total = _svc().list_dlq(ctx, limit=limit, offset=offset)
    return {"items": [i.__dict__ for i in items], "page": PageMeta(total=total, limit=limit, offset=offset).model_dump()}


@_router.post("/dlq/{task_id}/redrive")
async def redrive(task_id: str, body: RedriveRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().redrive_dlq(ctx, RedriveDLQCommand(task_id=task_id, tenant_id=body.tenant_id, idempotency_key=body.idempotency_key, reason=body.reason))
    except AdminError as exc:
        raise _err(exc) from exc


@_router.get("/approvals")
async def approvals(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return [a.__dict__ for a in _svc().list_approvals(ctx)]


@_router.get("/tenants")
async def tenants(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return [t.__dict__ for t in _svc().list_tenants(ctx)]


@_router.get("/audit")
async def audit(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant_scope: str | None = None,
    action: str | None = None,
):
    _require_admin(ctx)
    _no_cache(response)
    items, total = _svc().list_audit(ctx, limit=limit, offset=offset, tenant_scope=tenant_scope, action=action)
    return {"items": [i.__dict__ for i in items], "page": PageMeta(total=total, limit=limit, offset=offset).model_dump()}


@_router.get("/alerts")
async def alerts(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return [a.__dict__ for a in _svc().list_alerts(ctx)]


@_router.get("/providers")
async def providers(response: Response, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    _no_cache(response)
    return [p.__dict__ for p in _svc().list_providers(ctx)]


@_router.get("/production-integrations")
async def production_integrations(response: Response, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    _no_cache(response)
    return _svc().list_production_integrations(ctx)


@_router.get("/failures")
async def failures(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _require_admin(ctx)
    _no_cache(response)
    items, total = _svc().list_failures(ctx, limit=limit, offset=offset)
    return {"items": [i.__dict__ for i in items], "page": PageMeta(total=total, limit=limit, offset=offset).model_dump()}


@_router.get("/side-effects")
async def side_effects(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _require_admin(ctx)
    _no_cache(response)
    items, total = _svc().list_side_effects(ctx, limit=limit, offset=offset)
    return {"items": [i.__dict__ for i in items], "page": PageMeta(total=total, limit=limit, offset=offset).model_dump()}


@_router.post("/routing/activate")
async def routing_activate(body: ActivateRoutingRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        candidate = _svc().get_routing_candidate(body.candidate_id)
        if candidate is None:
            raise AdminError("admin_target_not_found")
        return _svc().activate_routing(
            ctx,
            ActivateRoutingCommand(
                candidate_id=body.candidate_id,
                expected_policy_version=body.expected_policy_version,
                confirmation_token=body.confirmation_token,
                reason=body.reason,
            ),
            candidate=candidate,
        )
    except AdminError as exc:
        raise _err(exc) from exc


@_router.post("/routing/rollback")
async def routing_rollback(body: RollbackRoutingRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().rollback_routing(ctx, RollbackRoutingCommand(confirmation_token=body.confirmation_token, reason=body.reason))
    except AdminError as exc:
        raise _err(exc) from exc


@_router.post("/approvals/{workflow_id}/{approval_id}/decide")
async def approval_decide(
    workflow_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _require_admin(ctx)
    try:
        return _svc().decide_approval(
            ctx,
            ApprovalDecisionCommand(
                workflow_id=workflow_id,
                approval_id=approval_id,
                decision=body.decision,
                idempotency_key=body.idempotency_key,
                reason=body.reason,
                tenant_id=body.tenant_id,
            ),
        )
    except AdminError as exc:
        raise _err(exc) from exc


@_router.post("/workers/resume")
async def resume(body: ResumeRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().worker_resume(ctx, reason=body.reason)
    except AdminError as exc:
        raise _err(exc) from exc


@_router.get("/routing/confirmation/{candidate_id}")
async def routing_confirmation(candidate_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return {"confirmation_token": confirmation_token(actor_ref=ctx.actor_ref(), action="routing.activate", target_id=candidate_id)}


@_router.get("/routing/rollback-confirmation")
async def routing_rollback_confirmation(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    return {"confirmation_token": confirmation_token(actor_ref=ctx.actor_ref(), action="routing.rollback", target_id="active")}


@_router.post("/workers/drain")
async def drain(body: DrainRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().worker_drain(ctx, reason=body.reason)
    except AdminError as exc:
        raise _err(exc) from exc


@_router.get("/controlled-launch/handoff")
async def controlled_launch_handoff(response: Response, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    _no_cache(response)
    return _svc().controlled_launch_handoff(ctx)


@_router.get("/controlled-launch/status")
async def controlled_launch_status(response: Response, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    _no_cache(response)
    return _svc().controlled_launch_status(ctx)


class LaunchKillRequest(BaseModel):
    policy_id: str
    reason: str = ""


@_router.post("/controlled-launch/kill")
async def controlled_launch_kill(body: LaunchKillRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().controlled_launch_kill(ctx, policy_id=body.policy_id, reason=body.reason)
    except AdminError as exc:
        raise _err(exc) from exc


@_router.get("/controlled-launch/evaluate")
async def controlled_launch_evaluate(response: Response, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)], candidate_id: str = ""):
    _require_admin(ctx)
    _no_cache(response)
    try:
        return _svc().controlled_launch_evaluate_gate(ctx, candidate_id=candidate_id)
    except AdminError as exc:
        raise _err(exc) from exc


@_router.get("/controlled-launch/{candidate_id}")
async def controlled_launch_read(candidate_id: str, response: Response, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    _no_cache(response)
    try:
        return _svc().controlled_launch_read(ctx, candidate_id)
    except AdminError as exc:
        raise _err(exc) from exc


class LaunchHoldRequest(BaseModel):
    reason: str = ""


@_router.post("/controlled-launch/{candidate_id}/hold")
async def controlled_launch_hold(candidate_id: str, body: LaunchHoldRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().controlled_launch_hold(ctx, candidate_id, reason=body.reason)
    except AdminError as exc:
        raise _err(exc) from exc


@_router.post("/controlled-launch/{candidate_id}/abort")
async def controlled_launch_abort(candidate_id: str, body: LaunchHoldRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().controlled_launch_abort(ctx, candidate_id, reason=body.reason)
    except AdminError as exc:
        raise _err(exc) from exc


@_router.post("/controlled-launch/{candidate_id}/rollback")
async def controlled_launch_rollback(candidate_id: str, body: LaunchHoldRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().controlled_launch_rollback(ctx, candidate_id, reason=body.reason)
    except AdminError as exc:
        raise _err(exc) from exc


@_router.get("/production-activation/preflight/{candidate_id}")
async def production_activation_preflight(candidate_id: str, response: Response, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    _no_cache(response)
    return _svc().production_activation_preflight(ctx, candidate_id)


@_router.get("/production-activation/{candidate_id}")
async def production_activation_read(candidate_id: str, response: Response, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    _no_cache(response)
    try:
        return _svc().production_activation_read(ctx, candidate_id)
    except AdminError as exc:
        raise _err(exc) from exc


@_router.post("/production-activation/{candidate_id}/rollback")
async def production_activation_rollback(candidate_id: str, body: LaunchHoldRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    _require_admin(ctx)
    try:
        return _svc().production_activation_rollback(ctx, candidate_id, reason=body.reason)
    except AdminError as exc:
        raise _err(exc) from exc
