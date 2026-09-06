"""FastAPI router — /api/v1/personalization.

Same auth/tenant boundary as every other Panda API surface
(security.api_auth.get_security_context) -- style/voice preferences are
scoped strictly to the authenticated (tenant_id, owner_id), never trusted
from client-supplied identity (Block 4.38).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from security.api_auth import get_security_context
from security.identity import RequestSecurityContext

from personalization.errors import PersonalizationError
from personalization.service import PersonalizationService

_router = APIRouter(prefix="/api/v1/personalization", tags=["personalization"])
_service: PersonalizationService | None = None


def configure_personalization_router(service: PersonalizationService) -> APIRouter:
    global _service
    _service = service
    return _router


def _svc() -> PersonalizationService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "personalization_unavailable"})
    return _service


def _err(exc: PersonalizationError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


class PreferencesResponse(BaseModel):
    style: str
    tone: str
    length: str
    language: str
    voice_id: str
    updated_at: str = ""


class PreferencesUpdateRequest(BaseModel):
    style: str | None = None
    tone: str | None = None
    length: str | None = None
    language: str | None = None
    voice_id: str | None = None


class VoiceEntry(BaseModel):
    voice_id: str
    label: str
    category: str


@_router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    prefs = _svc().get_preferences(tenant_id=ctx.tenant_id, owner_id=ctx.user_id)
    return PreferencesResponse(
        style=prefs.style,
        tone=prefs.tone,
        length=prefs.length,
        language=prefs.language,
        voice_id=prefs.voice_id,
        updated_at=prefs.updated_at,
    )


@_router.put("/preferences", response_model=PreferencesResponse)
async def put_preferences(
    body: PreferencesUpdateRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    try:
        prefs = _svc().set_preferences(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            style=body.style,
            tone=body.tone,
            length=body.length,
            language=body.language,
            voice_id=body.voice_id,
        )
    except PersonalizationError as exc:
        raise _err(exc) from exc
    return PreferencesResponse(
        style=prefs.style,
        tone=prefs.tone,
        length=prefs.length,
        language=prefs.language,
        voice_id=prefs.voice_id,
        updated_at=prefs.updated_at,
    )


@_router.get("/voices", response_model=list[VoiceEntry])
async def list_voices(ctx: Annotated[RequestSecurityContext, Depends(get_security_context)]):
    return [
        VoiceEntry(voice_id=v.voice_id, label=v.label, category=v.category) for v in _svc().list_voices()
    ]


@_router.post("/voices/{voice_id}/preview")
async def preview_voice(
    voice_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    try:
        blob, mime = _svc().preview_voice(voice_id=voice_id)
    except PersonalizationError as exc:
        raise _err(exc) from exc
    return Response(content=blob, media_type=mime, headers={"Cache-Control": "private, no-store"})
