"""HTTP API for accounts, login, owner management, compliance."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.responses import JSONResponse, RedirectResponse

from accounts.csrf import new_csrf_token, validate_csrf
from accounts.dual_auth import get_accounts_service, get_security_context_dual
from accounts.errors import AccountsError, InvalidCredentialsError
from accounts.models import CSRF_COOKIE_NAME, ROLE_OWNER, SESSION_COOKIE_NAME
from accounts.reasons import OWNER_REQUIRED
from security.identity import RequestSecurityContext

_router = APIRouter(tags=["accounts"])
_service = None


def configure_accounts_router(service) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc():
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "accounts_unavailable"})
    return _service


def _err(exc: AccountsError) -> HTTPException:
    code = 401
    if exc.code in {"ACCOUNT_DISABLED", "OWNER_REQUIRED", "TENANT_SCOPE_DENIED", "PROTECTED_OWNER", "CONSENT_REQUIRED"}:
        code = 403
    if exc.code in {"RATE_LIMITED"}:
        code = 429
    if exc.code in {"NOT_FOUND"}:
        code = 404
    if exc.code in {"USERNAME_TAKEN", "INVALID_USERNAME", "password_policy", "PASSWORD_POLICY"}:
        code = 400
    return HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message})


def _set_session_cookies(response: Response, session, *, secure: bool) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.session_id,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=session.csrf_token,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=10, max_length=128)
    accept_terms: bool = False
    accept_privacy: bool = False
    marketing_opt_in: bool = False  # must remain optional / unchecked by default


class OwnerCreateUserRequest(BaseModel):
    username: str
    role: str = "USER"
    tenant_id: str | None = None


class OwnerStatusRequest(BaseModel):
    status: str


class OwnerRoleRequest(BaseModel):
    role: str


class ComplimentaryGrantRequest(BaseModel):
    tenant_id: str
    user_id: str
    plan_id: str
    reason: str
    access_until: str = ""
    unlimited: bool = False


class DecisionRequest(BaseModel):
    decision_type: str
    decision: str = "ACCEPTED"
    document_type: str = ""
    document_version: str = ""


class PaymentRevokeRequest(BaseModel):
    provider: str
    provider_reference: str


async def _login_request_from_http(request: Request) -> LoginRequest:
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail={"code": "INVALID_CREDENTIALS"})
        return LoginRequest.model_validate(payload)
    form = await request.form()
    return LoginRequest(
        username=str(form.get("username") or ""),
        password=str(form.get("password") or ""),
    )


def _wants_html_login_redirect(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return False
    return "text/html" in accept


@_router.post("/api/accounts/login")
async def login(request: Request):
    svc = _svc()
    try:
        body = await _login_request_from_http(request)
        view, session = svc.login(
            username=body.username,
            password=body.password,
            source_ip=request.client.host if request.client else "",
        )
    except InvalidCredentialsError as exc:
        raise _err(exc) from exc
    except AccountsError as exc:
        raise _err(exc) from exc
    except HTTPException:
        raise
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(status_code=422, detail={"code": "INVALID_CREDENTIALS"}) from None
    if _wants_html_login_redirect(request):
        response: Response = RedirectResponse(url="/app", status_code=303)
    else:
        response = JSONResponse(view)
        response.headers["Cache-Control"] = "no-store"
    _set_session_cookies(response, session, secure=svc.secure_cookies)
    return response


@_router.post("/api/accounts/logout")
async def logout(request: Request, response: Response):
    svc = _svc()
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    session = svc.store.get_session(sid) if sid else None
    if session:
        try:
            validate_csrf(request, session_csrf=session.csrf_token)
        except AccountsError as exc:
            raise _err(exc) from exc
    svc.logout(sid)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return {"ok": True}


@_router.post("/api/accounts/register")
async def register(body: RegisterRequest):
    svc = _svc()
    try:
        return svc.register(
            username=body.username,
            password=body.password,
            accept_terms=body.accept_terms,
            accept_privacy=body.accept_privacy,
            marketing_opt_in=body.marketing_opt_in,
        )
    except AccountsError as exc:
        raise _err(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "password_policy", "message": str(exc)}) from exc


@_router.get("/api/accounts/me")
async def me(ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    svc = _svc()
    if ctx.auth_method != "session":
        # API-key callers get minimal safe view without inventing human account
        return {
            "authenticated": True,
            "auth_method": ctx.auth_method,
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            "role": None,
            "access_type": "MACHINE",
        }
    view = svc.me(user_id=ctx.user_id, tenant_id=ctx.tenant_id)
    view["auth_method"] = "session"
    return view


@_router.get("/api/accounts/plans")
async def plans():
    return {"items": _svc().plans()}


@_router.get("/api/accounts/access")
async def access_view(ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    if ctx.auth_method != "session":
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    return _svc().me(user_id=ctx.user_id, tenant_id=ctx.tenant_id)


@_router.get("/api/owner/users")
async def owner_users(ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    svc = _svc()
    user = svc.store.get_user(ctx.user_id)
    if user is None or user.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail={"code": OWNER_REQUIRED})
    return {"items": svc.owner_list_users(actor_user_id=ctx.user_id)}


@_router.post("/api/owner/users")
async def owner_create_user(
    body: OwnerCreateUserRequest,
    request: Request,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)],
):
    svc = _svc()
    user = svc.store.get_user(ctx.user_id)
    if user is None or user.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail={"code": OWNER_REQUIRED})
    session = getattr(request.state, "accounts_session", None) or (svc.store.get_session(ctx.session_id) if ctx.session_id else None)
    if session:
        try:
            validate_csrf(request, session_csrf=session.csrf_token)
        except AccountsError as exc:
            raise _err(exc) from exc
    try:
        return svc.owner_create_user(actor_user_id=ctx.user_id, username=body.username, role=body.role, tenant_id=body.tenant_id)
    except AccountsError as exc:
        raise _err(exc) from exc


@_router.post("/api/owner/users/{user_id}/status")
async def owner_set_status(
    user_id: str,
    body: OwnerStatusRequest,
    request: Request,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)],
):
    svc = _svc()
    actor = svc.store.get_user(ctx.user_id)
    if actor is None or actor.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail={"code": OWNER_REQUIRED})
    session = getattr(request.state, "accounts_session", None)
    if session:
        validate_csrf(request, session_csrf=session.csrf_token)
    try:
        updated = svc.identity.set_status(actor=actor, target_user_id=user_id, status=body.status)
        return svc.identity.safe_user_view(updated)
    except AccountsError as exc:
        raise _err(exc) from exc


@_router.post("/api/owner/users/{user_id}/role")
async def owner_set_role(
    user_id: str,
    body: OwnerRoleRequest,
    request: Request,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)],
):
    svc = _svc()
    actor = svc.store.get_user(ctx.user_id)
    if actor is None or actor.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail={"code": OWNER_REQUIRED})
    session = getattr(request.state, "accounts_session", None)
    if session:
        validate_csrf(request, session_csrf=session.csrf_token)
    try:
        updated = svc.identity.change_role(actor=actor, target_user_id=user_id, role=body.role)
        return svc.identity.safe_user_view(updated)
    except AccountsError as exc:
        raise _err(exc) from exc


@_router.post("/api/owner/users/{user_id}/revoke-sessions")
async def owner_revoke_sessions(user_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    svc = _svc()
    actor = svc.store.get_user(ctx.user_id)
    if actor is None or actor.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail={"code": OWNER_REQUIRED})
    count = svc.sessions.revoke_all_for_user(user_id, actor_id=ctx.user_id)
    return {"revoked": count}


@_router.post("/api/owner/complimentary")
async def owner_complimentary(body: ComplimentaryGrantRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    svc = _svc()
    actor = svc.store.get_user(ctx.user_id)
    if actor is None or actor.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail={"code": OWNER_REQUIRED})
    try:
        grant = svc.complimentary.grant(
            actor_id=actor.user_id,
            actor_role=actor.role,
            tenant_id=body.tenant_id,
            user_id=body.user_id,
            plan_id=body.plan_id,
            reason=body.reason,
            access_until=body.access_until,
            unlimited=body.unlimited,
        )
        return grant.__dict__
    except AccountsError as exc:
        raise _err(exc) from exc


@_router.post("/api/owner/complimentary/{grant_id}/revoke")
async def owner_comp_revoke(grant_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    svc = _svc()
    actor = svc.store.get_user(ctx.user_id)
    if actor is None or actor.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail={"code": OWNER_REQUIRED})
    try:
        return svc.complimentary.revoke(actor_id=actor.user_id, actor_role=actor.role, grant_id=grant_id).__dict__
    except AccountsError as exc:
        raise _err(exc) from exc


@_router.get("/api/owner/users/{user_id}/access")
async def owner_inspect_access(user_id: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    svc = _svc()
    actor = svc.store.get_user(ctx.user_id)
    if actor is None or actor.role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail={"code": OWNER_REQUIRED})
    target = svc.store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    return svc.access.safe_account_view(user_id=target.user_id, tenant_id=target.tenant_id)


# --- compliance / data rights ---

@_router.get("/api/accounts/legal/documents")
async def legal_documents():
    docs = _svc().compliance.store.list_policies()
    return {
        "items": [
            {
                "document_type": d.document_type,
                "version": d.version,
                "title": d.title,
                "status": d.status,
                "draft_requires_legal_review": d.draft_requires_legal_review,
                "content_reference": d.content_reference,
            }
            for d in docs
        ]
    }


@_router.get("/api/accounts/legal/ai-disclosure")
async def ai_disclosure():
    return _svc().compliance.ai_disclosure()


@_router.get("/api/accounts/compliance/inventory")
async def compliance_inventory():
    svc = _svc()
    return {
        "data_inventory": svc.compliance.inventory(),
        "retention": svc.compliance.retention_policy(),
        "cookies": svc.compliance.cookie_inventory(),
        "processors": svc.compliance.processor_inventory(),
        "age_policy_status": svc.compliance.age_policy_status(),
    }


@_router.post("/api/accounts/decisions")
async def record_decision(body: DecisionRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    if ctx.auth_method != "session":
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    rec = _svc().compliance.record_decision(
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        decision_type=body.decision_type,
        decision=body.decision,
        source="api",
        document_type=body.document_type,
        document_version=body.document_version,
    )
    return {
        "decision_id": rec.decision_id,
        "decision_type": rec.decision_type,
        "document_version": rec.document_version,
        "decision": rec.decision,
    }


@_router.post("/api/accounts/decisions/{decision_type}/withdraw")
async def withdraw_decision(decision_type: str, ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    if ctx.auth_method != "session":
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    rec = _svc().compliance.withdraw_decision(user_id=ctx.user_id, decision_type=decision_type)
    if rec is None:
        return {"withdrawn": False}
    return {"withdrawn": True, "decision_id": rec.decision_id}


@_router.get("/api/accounts/export")
async def export_data(ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    if ctx.auth_method != "session":
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    try:
        return _svc().compliance.export_account_data(user_id=ctx.user_id, tenant_id=ctx.tenant_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail={"code": "TENANT_SCOPE_DENIED"})


@_router.post("/api/accounts/deletion-request")
async def deletion_request(ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    if ctx.auth_method != "session":
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    req = _svc().compliance.request_deletion(user_id=ctx.user_id, tenant_id=ctx.tenant_id, actor_id=ctx.user_id)
    return {"request_id": req.request_id, "status": req.status, "retention_hold": req.retention_hold}


@_router.post("/api/accounts/payment-methods/revoke")
async def revoke_payment_method(body: PaymentRevokeRequest, ctx: Annotated[RequestSecurityContext, Depends(get_security_context_dual)]):
    if ctx.auth_method != "session":
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    control = _svc().payment_methods.revoke(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        provider=body.provider,
        provider_reference=body.provider_reference,
        source="user",
    )
    return {
        "usage_status": control.usage_status,
        "revoked_at": control.revoked_at,
        "provider_reference": control.provider_reference,
    }


@_router.get("/api/accounts/csrf")
async def csrf_bootstrap(request: Request, response: Response):
    """Issue CSRF cookie for anonymous forms; session login rotates token."""
    token = new_csrf_token()
    svc = _svc()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,
        secure=svc.secure_cookies,
        samesite="lax",
        path="/",
    )
    return {"csrf_token": token}
