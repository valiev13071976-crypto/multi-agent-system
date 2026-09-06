"""FastAPI router — /api/v1/business-assistant."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from artifacts.errors import ArtifactError
from business_assistant_api.errors import BusinessAssistantApiError
from business_assistant_api.models import API_VERSION
from business_assistant_api.service import BusinessAssistantApiService
from security.api_auth import get_audit_log, get_resource_authorizer, get_security_context
from security.identity import RequestSecurityContext
from security.rbac import PERM_ANALYZE_EXECUTE, PERM_HITL_APPROVE, PERM_WORKFLOW_CANCEL, PERM_WORKFLOW_READ

_router = APIRouter(prefix=f"/api/{API_VERSION}/business-assistant", tags=["business-assistant"])
_service: BusinessAssistantApiService | None = None
_upload_dir: str = ""
_media_provider: Any | None = None
_artifact_service: Any | None = None


def configure_business_assistant_api_router(
    service: BusinessAssistantApiService,
    *,
    upload_dir: str = "",
    media_provider: Any | None = None,
    artifact_service: Any | None = None,
) -> APIRouter:
    global _service, _upload_dir, _media_provider, _artifact_service
    _service = service
    _upload_dir = upload_dir or getattr(service, "upload_dir", "")
    _media_provider = media_provider
    _artifact_service = artifact_service or getattr(service, "artifact_service", None)
    return _router


def _artifacts() -> Any:
    if _artifact_service is None:
        raise HTTPException(status_code=503, detail={"code": "artifact_service_unavailable"})
    return _artifact_service


def _artifact_err(exc: ArtifactError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


def _content_disposition(kind: str, safe_name: str) -> str:
    import urllib.parse

    ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "file"
    quoted = urllib.parse.quote(safe_name)
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'


def _svc() -> BusinessAssistantApiService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "baa_unavailable"})
    return _service


def _err(exc: BusinessAssistantApiError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


class SubmitRequestBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=30000)
    artifact_refs: list[str] = Field(default_factory=list)
    requested_capability: str | None = None
    conversation_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    read_only: bool = False
    priority: str | None = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImageEditRequestBody(BaseModel):
    conversation_id: str = Field(..., min_length=1, max_length=128)
    instruction: str = Field(..., min_length=1, max_length=4000)


class ImageEditResponse(BaseModel):
    request_id: str
    text: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class ApproveRequestBody(BaseModel):
    approval_id: str | None = None
    plan_fingerprint: str | None = None


class RequestSummaryResponse(BaseModel):
    request_id: str
    status: str
    workflow_id: str = ""
    execution_id: str = ""
    correlation_id: str = ""
    trace_id: str = ""
    workload_class: str = "interactive"
    approval_required: bool = False
    approval_id: str = ""
    preview_id: str = ""
    conversation_id: str = ""


class EventResponse(BaseModel):
    event_id: str
    event_type: str
    timestamp: str
    workflow_id: str = ""
    stage: str = ""
    step: str = ""
    status: str = ""
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = ""


def _summary(rec) -> RequestSummaryResponse:
    return RequestSummaryResponse(
        request_id=rec.request_id,
        status=rec.status,
        workflow_id=rec.workflow_id,
        execution_id=rec.execution_id,
        correlation_id=rec.correlation_id,
        trace_id=rec.trace_id,
        workload_class=rec.workload_class,
        approval_required=rec.status == "WAITING_FOR_APPROVAL",
        approval_id=rec.approval_id,
        preview_id=rec.preview_id,
        conversation_id=rec.conversation_id,
    )


class ConversationResponse(BaseModel):
    conversation_id: str
    title: str
    created_at: str
    updated_at: str


class MessageItemResponse(BaseModel):
    message_id: str
    role: str
    content: str
    request_id: str = ""
    created_at: str
    artifact_refs: list[str] = Field(default_factory=list)


class UploadResponse(BaseModel):
    artifact_ref: str
    filename: str
    size_bytes: int
    mime_type: str
    artifact_id: str = ""
    kind: str = ""
    view_url: str = ""
    download_url: str = ""


class ArtifactMetadataResponse(BaseModel):
    artifact_id: str
    filename: str
    mime_type: str
    size_bytes: int
    kind: str
    created_at: str
    source: str
    status: str
    conversation_id: str = ""
    derived_from_artifact_id: str = ""
    view_url: str = ""
    download_url: str = ""


class CreateConversationBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class RenameConversationBody(BaseModel):
    title: str = Field(..., max_length=200)


@_router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    return [
        ConversationResponse(**c)
        for c in _svc().list_conversations(tenant_id=ctx.tenant_id, owner_id=ctx.user_id)
    ]


@_router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    body: CreateConversationBody | None,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    conv = _svc().create_conversation(
        tenant_id=ctx.tenant_id,
        owner_id=ctx.user_id,
        title=(body.title if body and body.title else "New chat"),
    )
    return ConversationResponse(
        conversation_id=conv.conversation_id,
        title=str(conv.metadata.get("title") or "Новый чат"),
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@_router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
async def rename_conversation(
    conversation_id: str,
    body: RenameConversationBody,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    try:
        conv = _svc().rename_conversation(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            conversation_id=conversation_id,
            title=body.title,
        )
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return ConversationResponse(
        conversation_id=conv.conversation_id,
        title=str(conv.metadata.get("title") or "Новый чат"),
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@_router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    try:
        _svc().delete_conversation(
            tenant_id=ctx.tenant_id, owner_id=ctx.user_id, conversation_id=conversation_id
        )
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return Response(status_code=204)


@_router.get("/conversations/{conversation_id}/messages", response_model=list[MessageItemResponse])
async def list_conversation_messages(
    conversation_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        msgs = _svc().get_conversation_messages(
            tenant_id=ctx.tenant_id, owner_id=ctx.user_id, conversation_id=conversation_id
        )
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return [MessageItemResponse(**m) for m in msgs]


@_router.post("/attachments", response_model=UploadResponse)
async def upload_attachment(
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    file: UploadFile = File(...),
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    data = await file.read()
    try:
        out = _svc().upload_attachment(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            filename=file.filename or "upload.bin",
            content=data,
            mime_type=file.content_type or "application/octet-stream",
            upload_base_dir=_upload_dir,
        )
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return UploadResponse(
        artifact_ref=out["artifact_ref"],
        filename=out["filename"],
        size_bytes=out["size_bytes"],
        mime_type=out["mime_type"],
        artifact_id=out.get("artifact_id", ""),
        kind=out.get("kind", ""),
        view_url=out.get("view_url", ""),
        download_url=out.get("download_url", ""),
    )


@_router.get("/media/{version_id}")
async def get_media(
    version_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    download: bool = Query(default=False),
):
    """Safe, tenant-authorized retrieval of a persisted generated image artifact.

    Reuses the existing auth boundary (session cookie for the Panda web UI, or
    X-API-Key/Bearer for other clients) plus tenant-scoped access checks already
    enforced by ProductMediaService — no new artifact or auth system.

    ``?download=1`` (3.5.7/3.5.8) additively switches the response to an
    explicit attachment disposition for the full-size-preview Download
    action; the default (omitted) behavior is byte-for-byte unchanged so
    existing inline ``<img>`` rendering keeps working exactly as before.
    """
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    provider = _media_provider
    if provider is None:
        raise HTTPException(status_code=404, detail={"code": "media_unavailable"})
    version = provider.get(tenant_id=ctx.tenant_id, version_id=version_id)
    if version is None:
        raise HTTPException(status_code=404, detail={"code": "media_not_found"})
    blob = provider.get_blob(tenant_id=ctx.tenant_id, version_id=version_id)
    if blob is None:
        raise HTTPException(status_code=404, detail={"code": "media_not_found"})
    mime = str(getattr(version, "mime_type", "") or "application/octet-stream")
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Length": str(len(blob)),
    }
    if download:
        from artifacts.validation import safe_download_filename

        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, "")
        name = safe_download_filename(f"image-{version_id}{ext}")
        headers["Content-Disposition"] = _content_disposition("attachment", name)
    return Response(content=blob, media_type=mime, headers=headers)


def _serve_artifact(artifact_id: str, ctx: RequestSecurityContext, *, disposition: str) -> Response:
    svc = _artifacts()
    try:
        rec, blob = svc.get_blob(tenant_id=ctx.tenant_id, artifact_id=artifact_id)
    except ArtifactError as exc:
        try:
            from artifacts.errors import ArtifactNotFoundError

            svc.record_download_failed(
                failure_category="not_found" if isinstance(exc, ArtifactNotFoundError) else "access_denied"
            )
        except Exception:
            pass
        raise _artifact_err(exc) from exc
    headers = {"Cache-Control": "private, no-store", "Content-Length": str(len(blob))}
    headers["Content-Disposition"] = _content_disposition(disposition, rec.safe_filename)
    if disposition == "attachment":
        svc.record_downloaded(rec)
    else:
        svc.record_opened(rec)
    return Response(content=blob, media_type=rec.mime_type, headers=headers)


@_router.get("/artifacts/{artifact_id:path}/download")
async def download_artifact(
    artifact_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    """Explicit original-bytes download (3.5.7) — Content-Disposition: attachment.

    ``artifact_id`` uses the ``:path`` converter (not the default single-
    segment matcher) because a legacy ``artifact://upload/...`` ref contains
    ``/`` -- registered *before* the bare metadata route below so a longer,
    more specific path always wins the match (Starlette resolves routes in
    registration order; a greedy ``:path`` metadata route checked first
    would otherwise swallow these suffixed paths too).
    """

    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    return _serve_artifact(artifact_id, ctx, disposition="attachment")


@_router.get("/artifacts/{artifact_id:path}/view")
async def view_artifact(
    artifact_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    """Inline/preview retrieval (3.5.6, 3.5.9) — browser-native rendering (e.g. PDF)."""

    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    return _serve_artifact(artifact_id, ctx, disposition="inline")


@_router.post("/artifacts/{artifact_id:path}/edit", response_model=ImageEditResponse)
async def edit_image_artifact(
    artifact_id: str,
    body: ImageEditRequestBody,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    """Direct "Редактировать" action (production acceptance defect closure):
    deterministic image.edit targeting exactly this canonical artifact --
    never the ambiguous "last generated image". Same PERM_ANALYZE_EXECUTE
    boundary as POST /requests (this also invokes a tool)."""

    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    get_audit_log().record(
        "baa.image_edit_requested",
        actor_ref=ctx.actor_ref(),
        tenant_ref=ctx.tenant_id,
        outcome="ok",
    )
    try:
        result = await _svc().edit_generated_image(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            conversation_id=body.conversation_id,
            source_artifact_id=artifact_id,
            instruction=body.instruction,
        )
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return ImageEditResponse(**result)


@_router.get("/artifacts/{artifact_id:path}", response_model=ArtifactMetadataResponse)
async def get_artifact_metadata(
    artifact_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    """Canonical artifact metadata (3.5.6) — tenant-scoped, fails closed."""

    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        rec = _artifacts().get_metadata(tenant_id=ctx.tenant_id, artifact_id=artifact_id)
    except ArtifactError as exc:
        raise _artifact_err(exc) from exc
    return ArtifactMetadataResponse(**rec.as_public_dict())


@_router.post("/requests", response_model=RequestSummaryResponse)
async def submit_request(
    body: SubmitRequestBody,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    get_audit_log().record(
        "baa.request_submitted",
        actor_ref=ctx.actor_ref(),
        tenant_ref=ctx.tenant_id,
        outcome="ok",
    )
    try:
        rec = await _svc().submit_async(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            message=body.message,
            artifact_refs=body.artifact_refs,
            requested_capability=body.requested_capability,
            conversation_id=body.conversation_id,
            idempotency_key=body.idempotency_key,
            read_only=body.read_only,
            priority=body.priority,
            metadata=body.metadata,
            trace_id=ctx.request_id,
        )
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return _summary(rec)


@_router.get("/requests/{request_id}", response_model=RequestSummaryResponse)
async def get_request(
    request_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        rec = _svc().get_request(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return _summary(rec)


@_router.get("/requests/{request_id}/status")
async def get_status(
    request_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        return _svc().get_status(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc


@_router.get("/requests/{request_id}/events", response_model=list[EventResponse])
async def list_events(
    request_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    after: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        events = _svc().list_events(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            request_id=request_id,
            after=after,
            limit=limit,
        )
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return [
        EventResponse(
            event_id=e.event_id,
            event_type=e.event_type,
            timestamp=e.timestamp,
            workflow_id=e.workflow_id,
            stage=e.stage,
            step=e.step,
            status=e.status,
            message=e.message,
            metadata=dict(e.metadata),
            correlation_id=e.correlation_id,
        )
        for e in events
    ]


@_router.get("/requests/{request_id}/result")
async def get_result(
    request_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        return _svc().get_result(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc


@_router.get("/requests/{request_id}/artifacts")
async def list_artifacts(
    request_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        return _svc().list_artifacts(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc


@_router.get("/requests/{request_id}/preview")
async def get_preview(
    request_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        return _svc().get_preview(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc


@_router.post("/requests/{request_id}/approve", response_model=RequestSummaryResponse)
async def approve_request(
    request_id: str,
    body: ApproveRequestBody,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    try:
        _svc().get_request(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    get_resource_authorizer().require_permission(ctx, PERM_HITL_APPROVE)
    get_audit_log().record(
        "baa.approval",
        actor_ref=ctx.actor_ref(),
        tenant_ref=ctx.tenant_id,
        resource_ref=request_id,
        outcome="ok",
    )
    try:
        rec = _svc().approve(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            request_id=request_id,
            approval_id=body.approval_id,
            plan_fingerprint=body.plan_fingerprint,
        )
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return _summary(rec)


@_router.post("/requests/{request_id}/reject", response_model=RequestSummaryResponse)
async def reject_request(
    request_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_HITL_APPROVE)
    try:
        rec = _svc().reject(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return _summary(rec)


@_router.post("/requests/{request_id}/cancel", response_model=RequestSummaryResponse)
async def cancel_request(
    request_id: str,
    response: Response,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    _no_cache(response)
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_CANCEL)
    try:
        rec = _svc().cancel(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except BusinessAssistantApiError as exc:
        raise _err(exc) from exc
    return _summary(rec)
