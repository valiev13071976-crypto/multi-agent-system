"""FastAPI router — /api/v1/voice."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from security.api_auth import get_resource_authorizer, get_security_context
from security.identity import RequestSecurityContext
from security.rbac import PERM_ANALYZE_EXECUTE, PERM_HITL_APPROVE, PERM_WORKFLOW_CANCEL, PERM_WORKFLOW_READ
from voice_interface.audio import validate_audio
from voice_interface.errors import VoiceInterfaceError
from voice_interface.service import VoiceInterfaceService

_router = APIRouter(prefix="/api/v1/voice", tags=["voice-interface"])
_service: VoiceInterfaceService | None = None


def configure_voice_interface_router(service: VoiceInterfaceService) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc() -> VoiceInterfaceService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "vi_unavailable"})
    return _service


def _err(exc: VoiceInterfaceError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


class TranscriptResponse(BaseModel):
    transcript: str
    language: str = "auto"


class VoiceRequestResponse(BaseModel):
    voice_request_id: str = ""
    request_id: str
    conversation_id: str = ""
    status: str
    transcript: str = ""
    text_result: str = ""
    tts_artifact_id: str = ""
    tts_mime_type: str = ""
    tts_error: str = ""
    preview: dict[str, Any] | None = None


@_router.post("/transcribe", response_model=TranscriptResponse)
async def transcribe(
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    file: UploadFile = File(...),
):
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    data = await file.read()
    try:
        audio = validate_audio(content=data, mime_type=file.content_type or "audio/wav", filename=file.filename or "audio.wav")
        result = _svc().transcribe(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, audio=audio)
    except VoiceInterfaceError as exc:
        raise _err(exc) from exc
    return TranscriptResponse(transcript=result.transcript, language=result.language)


@_router.post("/requests", response_model=VoiceRequestResponse)
async def submit_voice_request(
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    idempotency_key: str | None = Form(default=None),
    artifact_refs_json: str | None = Form(default=None),
    read_only: bool = Form(default=False),
):
    get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
    data = await file.read()
    refs: list[str] = []
    if artifact_refs_json:
        try:
            parsed = json.loads(artifact_refs_json)
            if isinstance(parsed, list):
                refs = [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass
    try:
        audio = validate_audio(content=data, mime_type=file.content_type or "audio/wav", filename=file.filename or "audio.wav")
        out = _svc().submit_voice_request(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            audio=audio,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            artifact_refs=refs,
            read_only=read_only,
        )
    except VoiceInterfaceError as exc:
        raise _err(exc) from exc
    return VoiceRequestResponse.model_validate(out)


@_router.get("/requests/{request_id}", response_model=VoiceRequestResponse)
async def get_voice_request(
    request_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        out = _svc().get_voice_request(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except VoiceInterfaceError as exc:
        raise _err(exc) from exc
    except Exception as exc:
        from business_assistant_api.errors import BusinessAssistantApiError

        if isinstance(exc, BusinessAssistantApiError):
            raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from exc
        raise
    return VoiceRequestResponse.model_validate(out)


@_router.post("/requests/{request_id}/approve", response_model=VoiceRequestResponse)
async def approve_voice_request(
    request_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_HITL_APPROVE)
    try:
        out = _svc().approve(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except VoiceInterfaceError as exc:
        raise _err(exc) from exc
    except Exception as exc:
        from business_assistant_api.errors import BusinessAssistantApiError

        if isinstance(exc, BusinessAssistantApiError):
            raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from exc
        raise
    return VoiceRequestResponse.model_validate(out)


@_router.post("/requests/{request_id}/reject", response_model=VoiceRequestResponse)
async def reject_voice_request(
    request_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_HITL_APPROVE)
    try:
        out = _svc().reject(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except VoiceInterfaceError as exc:
        raise _err(exc) from exc
    except Exception as exc:
        from business_assistant_api.errors import BusinessAssistantApiError

        if isinstance(exc, BusinessAssistantApiError):
            raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from exc
        raise
    return VoiceRequestResponse.model_validate(out)


@_router.post("/requests/{request_id}/cancel", response_model=VoiceRequestResponse)
async def cancel_voice_request(
    request_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_CANCEL)
    try:
        out = _svc().cancel(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, request_id=request_id)
    except VoiceInterfaceError as exc:
        raise _err(exc) from exc
    except Exception as exc:
        from business_assistant_api.errors import BusinessAssistantApiError

        if isinstance(exc, BusinessAssistantApiError):
            raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from exc
        raise
    return VoiceRequestResponse.model_validate(out)


@_router.get("/audio/{artifact_id}")
async def get_tts_audio(
    artifact_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_READ)
    try:
        blob, mime = _svc().get_tts_audio(
            tenant_id=ctx.tenant_id, owner_id=ctx.user_id, artifact_id=artifact_id
        )
    except VoiceInterfaceError as exc:
        raise _err(exc) from exc
    return Response(content=blob, media_type=mime, headers={"Cache-Control": "private, no-store"})
