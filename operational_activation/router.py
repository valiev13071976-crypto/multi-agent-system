"""HTTP surface for operational activation status (read-only; no live side effects)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from operational_activation.channels import evaluate_channel_access, telegram_live_boundary, voice_live_boundary
from operational_activation.hitl_write import HitlWriteGovernor
from operational_activation.product_definition import product_definition
from security.identity import RequestSecurityContext
from security.api_auth import get_security_context

_router = APIRouter(prefix="/api/v1/operational", tags=["operational-activation"])
_runtime = None


def configure_operational_activation_router(runtime) -> APIRouter:
    global _runtime
    _runtime = runtime
    return _router


def _rt():
    if _runtime is None:
        raise HTTPException(status_code=503, detail={"code": "operational_unavailable"})
    return _runtime


@_router.get("/status")
def operational_status() -> dict[str, Any]:
    return _rt().status()


@_router.get("/product-definition")
def product_def() -> dict[str, Any]:
    return product_definition()


@_router.get("/telegram/live-boundary")
def tg_boundary() -> dict[str, Any]:
    return telegram_live_boundary()


@_router.get("/voice/live-boundary")
def voice_boundary() -> dict[str, Any]:
    return voice_live_boundary()


class ChannelAccessRequest(BaseModel):
    channel: str = Field(..., pattern="^(telegram|voice|web)$")
    user_id: str | None = None
    tenant_id: str | None = None


@_router.post("/channel-access")
def channel_access(
    body: ChannelAccessRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
) -> dict[str, Any]:
    from accounts.dual_auth import get_accounts_service

    svc = get_accounts_service()
    if svc is None:
        raise HTTPException(status_code=503, detail={"code": "accounts_unavailable"})
    uid = body.user_id or ctx.user_id
    tid = body.tenant_id or ctx.tenant_id
    return evaluate_channel_access(accounts_service=svc, user_id=uid, tenant_id=tid, channel=body.channel)


class ProposeWriteRequest(BaseModel):
    action: str
    resource: str
    params: dict[str, Any] = Field(default_factory=dict)
    integration: str = "internal"
    idempotency_key: str


@_router.post("/hitl-write/propose")
def propose_write(
    body: ProposeWriteRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
) -> dict[str, Any]:
    gov: HitlWriteGovernor = _rt().write_governor
    prop = gov.propose(
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_ref(),
        action=body.action,
        resource=body.resource,
        params=body.params,
        integration=body.integration,
        idempotency_key=body.idempotency_key,
    )
    return prop.__dict__


class ApproveWriteRequest(BaseModel):
    proposal_id: str
    expected_fingerprint: str | None = None


@_router.post("/hitl-write/approve")
def approve_write(
    body: ApproveWriteRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
) -> dict[str, Any]:
    gov: HitlWriteGovernor = _rt().write_governor
    try:
        prop = gov.approve(
            proposal_id=body.proposal_id,
            approver_id=ctx.actor_ref(),
            tenant_id=ctx.tenant_id,
            expected_fingerprint=body.expected_fingerprint,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail={"code": "TENANT_SCOPE_DENIED"})
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": str(exc)})
    return prop.__dict__


class ExecuteWriteRequest(BaseModel):
    proposal_id: str
    params_now: dict[str, Any] | None = None


@_router.post("/hitl-write/execute")
def execute_write(
    body: ExecuteWriteRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
) -> dict[str, Any]:
    gov: HitlWriteGovernor = _rt().write_governor
    try:
        prop = gov.execute(proposal_id=body.proposal_id, tenant_id=ctx.tenant_id, params_now=body.params_now)
    except PermissionError:
        raise HTTPException(status_code=403, detail={"code": "TENANT_SCOPE_DENIED"})
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": str(exc)})
    return prop.__dict__
