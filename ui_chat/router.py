"""FastAPI router for UI Chat — Block 14 browser-facing API."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from security.api_auth import get_audit_log, get_resource_authorizer, get_security_context
from security.identity import RequestSecurityContext
from security.rbac import PERM_ANALYZE_EXECUTE, PERM_WORKFLOW_CANCEL, PERM_WORKFLOW_READ
from ui_chat.errors import UIChatError, CHAT_ACCESS_DENIED
from ui_chat.markdown import render_markdown_safe
from ui_chat.service import UIChatService

_router = APIRouter(prefix="/api/chat", tags=["chat"])
_service: UIChatService | None = None


def configure_ui_chat_router(service: UIChatService) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc() -> UIChatService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"error": "chat_unavailable"})
    return _service


def _chat_error(exc: UIChatError) -> HTTPException:
    status = 404 if exc.code in {CHAT_ACCESS_DENIED, "chat_not_found", "attachment_not_found", "task_not_found"} else 422
    from ui_chat.errors import CHAT_ACCESS_DENIED as _CAD

    if exc.code == _CAD or exc.code.endswith("_not_found"):
        status = 404
    return HTTPException(
        status_code=status,
        detail={
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
        },
    )


class ConversationCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationResponse(BaseModel):
    conversation_id: str
    title: str
    created_at: str
    updated_at: str
    status: str


class MessageResponse(BaseModel):
    message_id: str
    role: str
    content: str
    content_html: str
    content_version: int
    attachment_ids: list[str]
    created_at: str


class SubmitTurnRequest(BaseModel):
    text: str = Field(default="", max_length=30000)
    attachment_ids: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    mode: str = Field(default="both")
    role: str = Field(default="Judge")


class RunResponse(BaseModel):
    run_id: str
    conversation_id: str
    status: str
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class TranscriptResponse(BaseModel):
    transcript_id: str
    text: str


class TtsRequest(BaseModel):
    message_id: str
    voice: str = Field(default="default", max_length=64)


class TtsResponse(BaseModel):
    artifact_id: str
    mime_type: str
    byte_size: int


class AttachmentResponse(BaseModel):
    attachment_id: str
    filename: str
    attachment_class: str
    mime_type: str
    size_bytes: int
    status: str
    artifact_ref: str | None = None
    error_code: str | None = None


class TaskResponse(BaseModel):
    task_id: str
    conversation_id: str | None
    operation_label: str
    status: str
    phase: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancel_available: bool = False
    created_at: str
    finished_at: str | None = None


def _conv(c) -> ConversationResponse:
    return ConversationResponse(
        conversation_id=c.conversation_id,
        title=c.title,
        created_at=c.created_at,
        updated_at=c.updated_at,
        status=c.status,
    )


def _msg(m) -> MessageResponse:
    return MessageResponse(
        message_id=m.message_id,
        role=m.role,
        content=m.content,
        content_html=render_markdown_safe(m.content),
        content_version=m.content_version,
        attachment_ids=list(m.attachment_ids),
        created_at=m.created_at,
    )


def _run(r) -> RunResponse:
    return RunResponse(
        run_id=r.run_id,
        conversation_id=r.conversation_id,
        status=r.status,
        user_message_id=r.user_message_id,
        assistant_message_id=r.assistant_message_id,
        workflow_id=r.workflow_id,
        task_id=r.task_id,
        error_code=r.error_code,
        error_message=r.error_message,
    )


def _attach(a) -> AttachmentResponse:
    return AttachmentResponse(
        attachment_id=a.attachment_id,
        filename=a.filename_safe,
        attachment_class=a.attachment_class,
        mime_type=a.mime_type,
        size_bytes=a.size_bytes,
        status=a.status,
        artifact_ref=a.artifact_ref,
        error_code=a.error_code,
    )


def _task(t) -> TaskResponse:
    return TaskResponse(
        task_id=t.task_id,
        conversation_id=t.conversation_id,
        operation_label=t.operation_label,
        status=t.status,
        phase=t.phase,
        progress_current=t.progress_current,
        progress_total=t.progress_total,
        error_code=t.error_code,
        error_message=t.error_message,
        cancel_available=t.cancel_available,
        created_at=t.created_at,
        finished_at=t.finished_at,
    )


@_router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    body: ConversationCreateRequest | None = None,
):
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    req = body or ConversationCreateRequest()
    conv = _svc().create_conversation(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, title=req.title
    )
    return _conv(conv)


@_router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    return [_conv(c) for c in _svc().list_conversations(tenant_id=ctx.tenant_id, user_id=ctx.user_id)]


@_router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        conv = _svc().get_conversation(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id, conversation_id=conversation_id
        )
    except UIChatError as exc:
        raise _chat_error(exc) from exc
    return _conv(conv)


@_router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResponse])
async def list_messages(
    conversation_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        msgs = _svc().list_messages(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id, conversation_id=conversation_id
        )
    except UIChatError as exc:
        raise _chat_error(exc) from exc
    return [_msg(m) for m in msgs]


@_router.post("/conversations/{conversation_id}/turns", response_model=RunResponse)
async def submit_turn(
    conversation_id: str,
    body: SubmitTurnRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    get_audit_log().record(
        "chat.turn_submitted",
        actor_ref=ctx.actor_ref(),
        tenant_ref=ctx.tenant_id,
        outcome="ok",
    )
    try:
        run = await _svc().submit_turn(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            conversation_id=conversation_id,
            text=body.text,
            attachment_ids=tuple(body.attachment_ids),
            idempotency_key=body.idempotency_key,
            request_id=ctx.request_id,
            actor_ref=ctx.actor_ref(),
            mode=body.mode,
            role=body.role,
        )
    except UIChatError as exc:
        raise _chat_error(exc) from exc
    return _run(run)


@_router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        run = _svc().get_run(tenant_id=ctx.tenant_id, user_id=ctx.user_id, run_id=run_id)
    except UIChatError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    return _run(run)


@_router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_CANCEL)
    try:
        run = await _svc().cancel_run(tenant_id=ctx.tenant_id, user_id=ctx.user_id, run_id=run_id)
    except UIChatError as exc:
        raise _chat_error(exc) from exc
    return _run(run)


@_router.post("/attachments", response_model=AttachmentResponse)
async def upload_attachment(
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
):
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    data = await file.read()
    try:
        ref = _svc().upload_attachment(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            conversation_id=conversation_id,
            filename=file.filename or "upload.bin",
            mime_type=file.content_type or "application/octet-stream",
            data=data,
        )
    except UIChatError as exc:
        status = 413 if exc.code == "attachment_too_large" else 422
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message}) from exc
    return _attach(ref)


@_router.get("/attachments/{attachment_id}", response_model=AttachmentResponse)
async def get_attachment(
    attachment_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        ref = _svc().get_attachment(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id, attachment_id=attachment_id
        )
    except UIChatError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    return _attach(ref)


@_router.post("/voice/transcribe", response_model=TranscriptResponse)
async def transcribe_voice(
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    file: UploadFile = File(...),
):
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    data = await file.read()
    try:
        t = _svc().transcribe_voice(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            audio=data,
            mime_type=file.content_type or "audio/wav",
        )
    except UIChatError as exc:
        status = 413 if "too_large" in exc.code else 422
        raise HTTPException(status_code=status, detail={"code": exc.code, "retryable": exc.retryable}) from exc
    return TranscriptResponse(transcript_id=t.transcript_id, text=t.text)


@_router.post("/voice/synthesize", response_model=TtsResponse)
async def synthesize_voice(
    body: TtsRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    try:
        artifact = _svc().synthesize_voice(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            message_id=body.message_id,
            voice=body.voice,
        )
    except UIChatError as exc:
        raise HTTPException(status_code=404 if "not_found" in exc.code else 422, detail={"code": exc.code}) from exc
    return TtsResponse(
        artifact_id=artifact.artifact_id,
        mime_type=artifact.mime_type,
        byte_size=artifact.byte_size,
    )


@_router.get("/voice/audio/{artifact_id}")
async def get_voice_audio(
    artifact_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        artifact, blob = _svc().get_voice_audio(tenant_id=ctx.tenant_id, artifact_id=artifact_id)
    except UIChatError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    return Response(
        content=blob,
        media_type=artifact.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Length": str(len(blob)),
        },
    )


@_router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    return [_task(t) for t in _svc().list_tasks(tenant_id=ctx.tenant_id, user_id=ctx.user_id)]


@_router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        task = _svc().get_task(tenant_id=ctx.tenant_id, user_id=ctx.user_id, task_id=task_id)
    except UIChatError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc
    return _task(task)


@_router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_CANCEL)
    try:
        task = await _svc().cancel_task(tenant_id=ctx.tenant_id, user_id=ctx.user_id, task_id=task_id)
    except UIChatError as exc:
        raise _chat_error(exc) from exc
    return _task(task)
