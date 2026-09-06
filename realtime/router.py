"""FastAPI router — realtime WebSocket transport (Block 4.5).

Transport decision: WebSocket, not WebRTC. Reasoning (see the Block 4
delivery report for the full write-up): the existing, reused STT/TTS
provider interfaces (ui_chat.voice.stt.SpeechToTextProvider /
ui_chat.voice.tts.TextToSpeechProvider) and the conversation pipeline
(BusinessAssistantApiService.submit_async) are buffer-based request/response
calls, not native low-latency duplex media codecs -- WebRTC's value
proposition (SRTP media transport, jitter buffers, STUN/TURN NAT traversal)
targets a problem this architecture does not have yet. A single ordered,
bidirectional, authenticated WebSocket connection carrying JSON control/event
frames plus binary audio-chunk frames satisfies every actual requirement
(4.5): interruption (a control frame), reconnect (new connection + resume
token), text+audio simultaneity (interleaved frames on one connection), and
identical behavior across web/desktop-wrapper/mobile-webview targets (4.27)
with zero extra native/signalling-server infrastructure. If a genuinely
low-latency duplex audio codec/provider is adopted later, this transport
boundary (RealtimeSink in session.py) is the single seam to replace.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from accounts.dual_auth import get_accounts_service
from accounts.errors import AccountsError
from accounts.models import SESSION_COOKIE_NAME
from security.api_auth import get_auth_service
from security.auth import AuthService
from security.errors import SecurityError

from realtime.bridge import RealtimeConversationBridge
from realtime.errors import RealtimeError
from realtime.events import EV_ERROR, RealtimeEvent
from realtime.metrics import REALTIME_METRICS
from realtime.session import RealtimeSession

log = logging.getLogger(__name__)

_router = APIRouter(tags=["realtime"])
_bridge: RealtimeConversationBridge | None = None


def configure_realtime_router(bridge: RealtimeConversationBridge) -> APIRouter:
    global _bridge
    _bridge = bridge
    return _router


def _get_bridge() -> RealtimeConversationBridge:
    if _bridge is None:
        raise RuntimeError("realtime_bridge_unconfigured")
    return _bridge


class WebSocketSink:
    """realtime.session.RealtimeSink implementation over a live WebSocket.
    Swallows send failures on a dead/closing socket so the bridge's own
    session/turn bookkeeping never breaks on a transport-level error."""

    def __init__(self, websocket: WebSocket):
        self._ws = websocket

    async def send_event(self, event: RealtimeEvent) -> None:
        try:
            await self._ws.send_json({"kind": "event", **event.to_dict()})
        except Exception:
            pass

    async def send_audio(self, chunk: bytes, *, turn_id: str) -> None:
        try:
            await self._ws.send_bytes(chunk)
        except Exception:
            pass


def _extract_credential(websocket: WebSocket) -> tuple[str | None, str | None]:
    # Browsers cannot set arbitrary headers on the native WebSocket
    # handshake, so a query-param fallback is required for web clients;
    # non-browser clients may still use the standard X-API-Key/Authorization
    # headers. Either path is verified through the SAME AuthService as every
    # HTTP endpoint (Block 4.38) -- never a relaxed realtime-only auth path.
    api_key = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
    authorization = websocket.headers.get("authorization")
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if not bearer:
        token = websocket.query_params.get("token")
        if token:
            bearer = token
    return api_key, bearer


class _WsAuthFailed(Exception):
    """Uniform failure for realtime_ws's single fail-closed handler below,
    regardless of which of the two auth paths rejected the connection."""


async def _authenticate(websocket: WebSocket):
    """Block 4.38: the SAME dual-auth precedence every HTTP endpoint uses
    (accounts.dual_auth.get_security_context_dual) -- human session cookie
    first (sent automatically by the browser on the WS handshake request,
    exactly like any other same-origin fetch), then X-API-Key/bearer
    (machine/workspace key) via the existing AuthService. No realtime-only
    relaxed auth path, no browser-trusted tenant/owner identity."""

    client_ip = websocket.client.host if websocket.client else None
    accounts_service = get_accounts_service()
    session_id = websocket.cookies.get(SESSION_COOKIE_NAME)
    if session_id and accounts_service is not None:
        try:
            session = accounts_service.sessions.resolve(session_id)
            user = accounts_service.store.get_user(session.user_id)
            if user is None or user.status != "ACTIVE":
                raise AccountsError("AUTH_REQUIRED")
            return accounts_service.sessions.to_security_context(
                session=session,
                product_role=user.role,
                request_id=str(uuid.uuid4()),
                source_ip=client_ip,
            )
        except AccountsError as exc:
            raise _WsAuthFailed(str(exc)) from exc

    api_key, bearer = _extract_credential(websocket)
    auth: AuthService = get_auth_service()
    try:
        return auth.authenticate(api_key=api_key, bearer=bearer, source_ip=client_ip)
    except SecurityError as exc:
        raise _WsAuthFailed(str(exc)) from exc


@_router.websocket("/api/v1/realtime/ws")
async def realtime_ws(websocket: WebSocket) -> None:
    try:
        ctx = await _authenticate(websocket)
    except _WsAuthFailed:
        REALTIME_METRICS.inc_error("auth_failed")
        # Fail closed BEFORE accept() -- no session, no sink, no bridge
        # interaction for an unauthenticated caller.
        await websocket.close(code=4401)
        return

    await websocket.accept()
    bridge = _get_bridge()
    sink = WebSocketSink(websocket)

    conversation_id = websocket.query_params.get("conversation_id") or None
    voice_id = websocket.query_params.get("voice_id") or None
    resume_session_id = websocket.query_params.get("session_id") or None

    session: RealtimeSession | None = None
    if resume_session_id:
        session = await bridge.resume_session(
            resume_session_id, tenant_id=ctx.tenant_id, owner_id=ctx.user_id, sink=sink
        )
    if session is None:
        session = await bridge.create_session(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            sink=sink,
            conversation_id=conversation_id,
            voice_id=voice_id,
        )

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                bridge.mark_disconnected(session)
                return
            raw_bytes = message.get("bytes")
            if raw_bytes is not None:
                await bridge.on_audio_chunk(session, raw_bytes)
                continue
            raw_text = message.get("text")
            if raw_text is not None:
                await _handle_control_frame(bridge, session, raw_text)
                continue
    except WebSocketDisconnect:
        bridge.mark_disconnected(session)
    except Exception:
        log.exception("realtime_ws_loop_error")
        REALTIME_METRICS.inc_error("unknown")
        bridge.mark_disconnected(session)


async def _handle_control_frame(
    bridge: RealtimeConversationBridge, session: RealtimeSession, raw_text: str
) -> None:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        REALTIME_METRICS.inc_error("invalid_frame")
        return
    if not isinstance(payload, dict):
        REALTIME_METRICS.inc_error("invalid_frame")
        return

    kind = str(payload.get("type") or "").strip()
    client_turn_id = str(payload.get("client_turn_id") or "")
    try:
        if kind == "audio.commit":
            await bridge.on_audio_commit(session, client_turn_id=client_turn_id)
        elif kind == "text.message":
            await bridge.on_text_message(
                session, text=str(payload.get("text") or ""), client_turn_id=client_turn_id
            )
        elif kind == "barge_in":
            await bridge.barge_in(session)
        elif kind == "voice.select":
            await bridge.select_voice(session, str(payload.get("voice_id") or ""))
        elif kind == "session.close":
            await bridge.close_session(session, reason="client_requested")
        else:
            REALTIME_METRICS.inc_error("invalid_frame")
    except RealtimeError as exc:
        await session.sink.send_event(
            session.events.build(EV_ERROR, turn_id="", code=exc.code, message=exc.message)
        )
