"""Block 4 — RealtimeConversationBridge.

The ONE orchestrator between a realtime transport (realtime/router.py) and
the existing, unchanged Business Assistant conversation pipeline
(business_assistant_api.service.BusinessAssistantApiService). Voice is
another input/output modality on top of the exact same
submit_async()/get_result()/approve()/reject()/cancel() entry points the
text chat UI already uses -- same conversation persistence, same
ToolGateway/HITL/idempotency boundary, same personalization resolution.

Honesty note on "streaming" (Block 4.4/4.10, spec section 59/DELIVERY item
10/15): the underlying conversation pipeline and TTS provider interface
(ui_chat.voice.tts.TextToSpeechProvider.synthesize) are buffer-based --
submit_async() returns one complete answer, synthesize() returns one
complete audio buffer. This bridge streams both to the client as ordered,
non-overlapping chunks over the realtime transport (so the wire protocol,
event contract, and UI are genuinely incremental and forward-compatible
with a future token-level/audio-level streaming provider), but it does not
fabricate provider-side token/audio streaming that does not exist here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from business_assistant_api.models import (
    ST_BLOCKED,
    ST_CANCELLED,
    ST_COMPLETED,
    ST_REJECTED,
    ST_WAITING_FOR_APPROVAL,
)
from business_assistant_api.service import BusinessAssistantApiService
from security.redaction import redact
from security.tenant import require_tenant_id
from ui_chat.voice.stt import SpeechToTextProvider
from ui_chat.voice.tts import TextToSpeechProvider
from voice_interface.approval_intent import (
    is_cancel_intent,
    is_explicit_approval_intent,
    is_reject_intent,
    normalize_transcript,
)

from finops.models import UsageRecord
from finops.service import FinOpsService
from personalization.models import DEFAULT_VOICE_ID
from personalization.service import PersonalizationService
from realtime.errors import (
    RT_AUDIO_EMPTY,
    RT_CONVERSATION_UNAVAILABLE,
    RT_STT_EMPTY,
    RT_STT_FAILED,
    RT_TTS_FAILED,
    RealtimeError,
)
from realtime.events import (
    EV_ASSISTANT_AUDIO_COMPLETED,
    EV_ASSISTANT_AUDIO_DELTA,
    EV_ASSISTANT_STATUS,
    EV_ASSISTANT_TEXT_COMPLETED,
    EV_ASSISTANT_TEXT_DELTA,
    EV_ERROR,
    EV_INTERRUPTION,
    EV_SESSION_CLOSED,
    EV_SESSION_CONNECTED,
    EV_SESSION_RECONNECTED,
    EV_SESSION_STARTED,
    EV_TOOL_COMPLETED,
    EV_TOOL_STARTED,
    EV_USER_AUDIO_STARTED,
    EV_USER_TRANSCRIPT_FINAL,
    EV_USER_TRANSCRIPT_PARTIAL,
    EV_USER_TURN_COMMITTED,
    STATUS_ANALYZING_FILE,
    STATUS_GENERATING_IMAGE,
    STATUS_PREPARING_ANSWER,
    STATUS_THINKING,
)
from realtime.metrics import REALTIME_METRICS
from realtime.session import RealtimeSession, RealtimeSink, new_session_id
from realtime.state_machine import SessionEvent, SessionState

_TEXT_CHUNK_CHARS = 48
_AUDIO_CHUNK_BYTES = 4096
_TERMINAL_FAILURE_STATES = frozenset({ST_BLOCKED, ST_REJECTED, ST_CANCELLED})
_INTERRUPTIBLE_STATES = frozenset(
    {SessionState.THINKING, SessionState.ASSISTANT_STREAMING_TEXT, SessionState.ASSISTANT_SPEAKING}
)

# Production voice defect closure section 20: structured, SAFE
# (no raw audio/keys/full transcript beyond what already streams to the
# client anyway) per-turn diagnostic events, so a real production failure's
# exact broken boundary is visible from logs alone without another Cursor
# round. Reuses the standard `logging` module (same pattern as
# realtime/router.py's `log = logging.getLogger(__name__)`) instead of
# inventing a second event-bus architecture.
_voice_log = logging.getLogger("realtime.voice_diagnostics")


def _log_voice_event(event: str, *, session: RealtimeSession, turn_id: str = "", **fields: Any) -> None:
    try:
        payload = {
            "event": event,
            "conversation_id": session.conversation_id,
            "session_id": session.session_id,
            "turn_id": turn_id or session.current_turn_id,
        }
        payload.update(fields)
        _voice_log.info("realtime_voice_event", extra={"voice_event": payload})
    except Exception:
        pass


def _chunk_text(text: str, size: int = _TEXT_CHUNK_CHARS) -> list[str]:
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def _chunk_bytes(data: bytes, size: int = _AUDIO_CHUNK_BYTES) -> list[bytes]:
    if not data:
        return []
    return [data[i : i + size] for i in range(0, len(data), size)]


def _status_for_artifacts(artifacts: list[dict]) -> str:
    kinds = {str(a.get("artifact_type") or a.get("type") or "") for a in artifacts}
    if "image" in kinds:
        return STATUS_GENERATING_IMAGE
    if artifacts:
        return STATUS_ANALYZING_FILE
    return STATUS_PREPARING_ANSWER


class RealtimeConversationBridge:
    def __init__(
        self,
        *,
        ba_api: BusinessAssistantApiService,
        stt: SpeechToTextProvider,
        tts: TextToSpeechProvider,
        personalization: PersonalizationService | None = None,
        finops: FinOpsService | None = None,
    ):
        self.ba_api = ba_api
        self.stt = stt
        self.tts = tts
        self.personalization = personalization
        # Block 4.37: the SAME shared FinOpsService the rest of the platform
        # attributes model/provider cost to (agents.router_v2.RouterV2.finops,
        # wired in main.py) -- never a second, realtime-only cost ledger.
        # Optional so existing callers/tests that never wire it keep working.
        self.finops = finops
        self._sessions: dict[str, RealtimeSession] = {}

    def _record_speech_usage(self, session: RealtimeSession, *, capability: str, turn_id: str = "") -> None:
        """Block 4.37: best-effort STT/TTS cost attribution by
        (tenant, owner/session, provider, capability) -- never blocks or
        fails a turn; no fabricated token/cost numbers (the reused STT/TTS
        provider interfaces do not report token counts, so cost is recorded
        as "unknown" here, exactly the existing FinOpsService unknown-cost
        semantics already used elsewhere, not a Block-4-invented policy)."""

        if self.finops is None:
            return
        try:
            self.finops.record_usage(
                UsageRecord(
                    task_id=turn_id or session.session_id,
                    provider_id=f"speech_{capability}",
                    model_id="realtime",
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    estimated_cost=None,
                    currency="USD",
                    timestamp=datetime.now(timezone.utc),
                    tenant_id=session.tenant_id,
                    user_id=session.owner_id,
                    request_id=turn_id,
                )
            )
        except Exception:
            pass

    # --- session lifecycle -------------------------------------------------

    def get_session(self, session_id: str) -> RealtimeSession | None:
        return self._sessions.get(session_id)

    async def create_session(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        sink: RealtimeSink,
        conversation_id: str | None = None,
        voice_id: str | None = None,
        language_hint: str = "ru",
        mime_type: str = "audio/webm",
    ) -> RealtimeSession:
        tenant = require_tenant_id(tenant_id)
        owner = str(owner_id or "").strip()
        conv = str(conversation_id or "").strip()
        if not conv:
            # Block 4.1/4.16: a realtime turn is a conversation turn like any
            # other -- it must have a real, persisted conversation_id so
            # BusinessAssistantApiService.submit_async's existing
            # history/persistence machinery actually records it (an empty
            # conversation_id silently skips message persistence there).
            conv_rec = self.ba_api.create_conversation(
                tenant_id=tenant, owner_id=owner, title="Голосовой чат"
            )
            conv = conv_rec.conversation_id

        resolved_voice = str(voice_id or "").strip()
        if not resolved_voice and self.personalization is not None:
            try:
                resolved_voice = self.personalization.get_preferences(
                    tenant_id=tenant, owner_id=owner
                ).voice_id
            except Exception:
                resolved_voice = ""
        resolved_voice = resolved_voice or DEFAULT_VOICE_ID

        session = RealtimeSession(
            session_id=new_session_id(),
            tenant_id=tenant,
            owner_id=owner,
            conversation_id=conv,
            voice_id=resolved_voice,
            sink=sink,
            language_hint=language_hint,
            mime_type=str(mime_type or "audio/webm"),
        )
        self._sessions[session.session_id] = session
        REALTIME_METRICS.inc("session_started")
        _log_voice_event(
            "voice_session_started",
            session=session,
            mime_type=session.mime_type,
        )

        session.state_machine.transition(SessionEvent.CONNECT)
        await session.sink.send_event(
            session.events.build(EV_SESSION_STARTED, conversation_id=conv, voice_id=resolved_voice)
        )
        session.state_machine.transition(SessionEvent.CONNECTED)
        await session.sink.send_event(session.events.build(EV_SESSION_CONNECTED))
        return session

    async def resume_session(
        self, session_id: str, *, tenant_id: str, owner_id: str, sink: RealtimeSink
    ) -> RealtimeSession | None:
        """Block 4.23 reconnect: reattach a NEW transport connection's sink to
        an existing in-memory session if it is still alive and owned by the
        SAME authenticated tenant/owner (never trusts a client-supplied
        session_id across tenants). Conversation context itself is never at
        risk even on a cache miss -- it lives in durable storage keyed by
        conversation_id, not in this in-memory session object."""

        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.tenant_id != require_tenant_id(tenant_id) or session.owner_id != str(owner_id or ""):
            return None
        t0 = time.monotonic()
        session.sink = sink
        if session.state_machine.can(SessionEvent.RECONNECTED):
            session.state_machine.transition(SessionEvent.RECONNECTED)
        elif session.state_machine.state != SessionState.LISTENING:
            # Not mid-reconnect bookkeeping (e.g. transport dropped without a
            # clean disconnect event) -- fail closed to a known-good state
            # rather than guessing.
            session.state_machine.state = SessionState.LISTENING
        await session.sink.send_event(session.events.build(EV_SESSION_RECONNECTED))
        REALTIME_METRICS.inc("session_reconnected")
        REALTIME_METRICS.reconnect_duration.observe((time.monotonic() - t0) * 1000)
        return session

    def record_playback_event(self, session: RealtimeSession, *, stage: str, turn_id: str = "") -> None:
        """Client->server playback telemetry ack (section 11/20): the
        browser is the only party that actually knows when audible
        playback started/completed, so realtime.js sends a lightweight
        `playback_event` control frame (same JSON-control-frame convention
        as audio.commit/barge_in/voice.select -- no new transport/
        architecture) at those two DOM audio-element events, logged here so
        a real production failure between "TTS audio received" and "user
        actually heard it" is still visible from server-side logs alone."""

        event = "audio_playback_started" if stage == "started" else "audio_playback_completed"
        _log_voice_event(event, session=session, turn_id=turn_id)

    async def select_voice(self, session: RealtimeSession, voice_id: str) -> None:
        """Block 4.29.2: applies immediately to subsequent TTS output in this
        session AND persists as the user's canonical voice preference
        (SAME PersonalizationService text-chat settings write). Never
        touches conversation history or tool/agent behavior (Block 4.29.3)."""

        vid = str(voice_id or "").strip().lower()
        if not vid:
            return
        session.voice_id = vid
        if self.personalization is not None:
            try:
                self.personalization.set_preferences(
                    tenant_id=session.tenant_id, owner_id=session.owner_id, voice_id=vid
                )
            except Exception:
                pass

    def mark_disconnected(self, session: RealtimeSession) -> None:
        """Abrupt transport loss (not an explicit close). The in-flight task
        (if any) is left running -- Stage A (submit_async) is shielded and
        keeps persisting to durable conversation storage regardless; Stage B
        (local text/audio chunk streaming) will simply fail to reach a dead
        sink, which the sink implementation swallows (see router.py)."""

        if session.state_machine.can(SessionEvent.DISCONNECTED):
            session.state_machine.transition(SessionEvent.DISCONNECTED)

    async def close_session(self, session: RealtimeSession, *, reason: str = "client_close") -> None:
        if session.closed:
            return
        if session.current_task is not None and not session.current_task.done():
            session.current_task.cancel()
            try:
                await session.current_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        session.closed = True
        if session.state_machine.can(SessionEvent.CLOSE):
            session.state_machine.transition(SessionEvent.CLOSE)
        try:
            await session.sink.send_event(session.events.build(EV_SESSION_CLOSED, reason=reason))
        except Exception:
            pass
        self._sessions.pop(session.session_id, None)
        REALTIME_METRICS.inc("session_ended")
        _log_voice_event("voice_session_closed", session=session, reason=reason)

    # --- voice input ---------------------------------------------------

    async def on_audio_chunk(self, session: RealtimeSession, chunk: bytes) -> None:
        if not chunk:
            return
        if session.state_machine.state in _INTERRUPTIBLE_STATES:
            await self.barge_in(session)

        first = not session.audio_buffer
        session.audio_buffer.extend(chunk)
        if first:
            session.turn_started_monotonic = time.monotonic()
            session.first_transcript_recorded = False
            await session.sink.send_event(session.events.build(EV_USER_AUDIO_STARTED))
            _log_voice_event("voice_capture_started", session=session, mime_type=session.mime_type)
        if session.state_machine.can(SessionEvent.AUDIO_STARTED):
            session.state_machine.transition(SessionEvent.AUDIO_STARTED)

        # Block 4.7/4.8: incremental partial transcript. The current STT
        # provider interface (ui_chat.voice.stt.SpeechToTextProvider) is
        # buffer-based, not natively streaming, so "incremental" here means
        # re-transcribing the growing buffer on each chunk with the SAME
        # provider a final commit uses -- correct and deterministic, but a
        # native streaming STT provider would replace this with true
        # token-level partials without changing the event contract.
        try:
            partial = normalize_transcript(
                self.stt.transcribe(
                    audio=bytes(session.audio_buffer),
                    mime_type=session.mime_type,
                    language=session.language_hint,
                )
            )
        except Exception:
            partial = ""
        if partial and partial != session.last_partial_transcript:
            session.last_partial_transcript = partial
            await session.sink.send_event(session.events.build(EV_USER_TRANSCRIPT_PARTIAL, text=partial))
            if not session.first_transcript_recorded and session.turn_started_monotonic is not None:
                REALTIME_METRICS.mic_to_first_transcript.observe(
                    (time.monotonic() - session.turn_started_monotonic) * 1000
                )
                session.first_transcript_recorded = True

    async def on_audio_commit(self, session: RealtimeSession, *, client_turn_id: str = "") -> str | None:
        if not session.audio_buffer:
            _log_voice_event("voice_capture_completed", session=session, audio_bytes=0, stage="empty_audio")
            raise RealtimeError(RT_AUDIO_EMPTY, http_status=422)
        audio_bytes = bytes(session.audio_buffer)
        session.audio_buffer.clear()
        session.last_partial_transcript = ""
        _log_voice_event(
            "voice_capture_completed",
            session=session,
            audio_bytes=len(audio_bytes),
            mime_type=session.mime_type,
        )
        if session.state_machine.can(SessionEvent.AUDIO_COMMITTED):
            session.state_machine.transition(SessionEvent.AUDIO_COMMITTED)

        stt_started = time.monotonic()
        _log_voice_event(
            "stt_request_started",
            session=session,
            provider=type(self.stt).__name__,
            audio_bytes=len(audio_bytes),
            mime_type=session.mime_type,
        )
        try:
            text = normalize_transcript(
                self.stt.transcribe(
                    audio=audio_bytes, mime_type=session.mime_type, language=session.language_hint
                )
            )
            REALTIME_METRICS.inc("stt_call")
            self._record_speech_usage(session, capability="stt")
            _log_voice_event(
                "stt_request_completed",
                session=session,
                provider=type(self.stt).__name__,
                success=True,
                latency_ms=round((time.monotonic() - stt_started) * 1000, 1),
                transcript_chars=len(text),
            )
        except Exception as exc:
            REALTIME_METRICS.inc_error("stt_failed")
            _log_voice_event(
                "stt_request_completed",
                session=session,
                provider=type(self.stt).__name__,
                success=False,
                latency_ms=round((time.monotonic() - stt_started) * 1000, 1),
                error_type=type(exc).__name__,
            )
            await session.sink.send_event(
                session.events.build(EV_ERROR, code=RT_STT_FAILED, message="stt_failed")
            )
            if session.state_machine.can(SessionEvent.ERROR):
                session.state_machine.transition(SessionEvent.ERROR)
            return None
        if not text:
            REALTIME_METRICS.inc_error("stt_failed")
            await session.sink.send_event(
                session.events.build(EV_ERROR, code=RT_STT_EMPTY, message="empty_transcript")
            )
            if session.state_machine.can(SessionEvent.ERROR):
                session.state_machine.transition(SessionEvent.ERROR)
            return None

        # Exactly ONE committed user turn per commit (Block 55.A): the final
        # transcript becomes the canonical turn text; no partials were ever
        # persisted as separate turns above.
        _log_voice_event("stt_transcript_received", session=session, transcript_chars=len(text))
        await session.sink.send_event(session.events.build(EV_USER_TRANSCRIPT_FINAL, text=text))
        if session.state_machine.can(SessionEvent.TRANSCRIPT_FINAL):
            session.state_machine.transition(SessionEvent.TRANSCRIPT_FINAL)

        if is_explicit_approval_intent(text) or is_reject_intent(text) or is_cancel_intent(text):
            return await self._handle_spoken_confirmation(session, text)
        return await self.commit_turn(session, text=text, client_turn_id=client_turn_id, is_voice=True)

    # --- text input (Block 4.14: voice <-> text switching) ----------------

    async def on_text_message(
        self, session: RealtimeSession, *, text: str, client_turn_id: str = ""
    ) -> str | None:
        cleaned = str(text or "").strip()
        if not cleaned:
            return None
        if session.state_machine.state in _INTERRUPTIBLE_STATES:
            await self.barge_in(session)
        if session.state_machine.can(SessionEvent.TEXT_MESSAGE):
            session.state_machine.transition(SessionEvent.TEXT_MESSAGE)
        return await self.commit_turn(session, text=cleaned, client_turn_id=client_turn_id, is_voice=False)

    # --- shared turn commit / dispatch --------------------------------------

    def _resolve_turn_id(self, session: RealtimeSession, client_turn_id: str) -> str:
        cid = str(client_turn_id or "").strip()
        if cid:
            # Block 4.23/55.F: must be stable across reconnect. A reconnect
            # attaches a BRAND NEW RealtimeSession (new session_id) to the
            # SAME conversation, so this id is scoped by conversation_id
            # (stable across reconnect), never by session_id (not stable) --
            # otherwise a client retry after reconnect would mint a
            # different idempotency_key and submit_async's own dedupe
            # (business_assistant_api.service._submit_prepare) would never
            # catch it, defeating the "no accidental repeated external
            # action" guarantee this id exists to provide.
            return f"rtc_{session.conversation_id[:12]}_{cid}"[:120]
        return session.next_turn_id()

    async def commit_turn(
        self, session: RealtimeSession, *, text: str, client_turn_id: str = "", is_voice: bool
    ) -> str:
        turn_id = self._resolve_turn_id(session, client_turn_id)
        if turn_id in session.committed_turn_ids:
            # Block 4.23/55.F: reconnect-triggered client retry of the same
            # logical turn is a safe no-op here; submit_async's own
            # idempotency_key dedupe (see business_assistant_api.service.
            # _submit_prepare) is the authoritative guarantee even if this
            # in-memory set were ever lost (e.g. process restart).
            return turn_id
        session.committed_turn_ids.add(turn_id)
        # One canonical ownership rule for committing a voice/text turn
        # (production voice defect closure section 7): this is the ONLY
        # place EV_USER_TURN_COMMITTED is ever sent for a NEW turn_id -- the
        # `turn_id in session.committed_turn_ids` guard above is the single
        # enforcement point, keyed by the SAME turn_id/idempotency boundary
        # business_assistant_api.service.submit_async's own dedupe uses
        # below, so one physical utterance/message can never become more
        # than one canonical user turn even across retries/reconnects.
        _log_voice_event(
            "voice_turn_committed", session=session, turn_id=turn_id, modality="voice" if is_voice else "text"
        )

        await session.sink.send_event(
            session.events.build(
                EV_USER_TURN_COMMITTED, turn_id=turn_id, text=text, modality="voice" if is_voice else "text"
            )
        )
        if session.state_machine.can(SessionEvent.TURN_COMMITTED):
            session.state_machine.transition(SessionEvent.TURN_COMMITTED)
        await session.sink.send_event(
            session.events.build(EV_ASSISTANT_STATUS, turn_id=turn_id, status=STATUS_THINKING)
        )

        turn_start = time.monotonic()
        _log_voice_event("assistant_response_started", session=session, turn_id=turn_id)
        task = asyncio.create_task(self._run_turn(session, turn_id=turn_id, text=text, turn_start=turn_start))
        session.current_task = task
        session.current_turn_id = turn_id
        return turn_id

    async def _submit(self, session: RealtimeSession, *, turn_id: str, text: str) -> tuple[str, list[dict]]:
        rec = await self.ba_api.submit_async(
            tenant_id=session.tenant_id,
            owner_id=session.owner_id,
            message=text,
            conversation_id=session.conversation_id,
            idempotency_key=turn_id,
            trace_id=f"realtime-{turn_id}",
        )
        if rec.status == ST_WAITING_FOR_APPROVAL:
            return "Это действие требует подтверждения.", []
        if rec.status == ST_COMPLETED:
            result = self.ba_api.get_result(
                tenant_id=session.tenant_id, owner_id=session.owner_id, request_id=rec.request_id
            )
            reply = str(result.get("final_answer") or result.get("summary") or "").strip()
            artifacts = list(result.get("artifacts") or [])
            return (reply or "Готово."), artifacts
        if rec.status in _TERMINAL_FAILURE_STATES:
            return (rec.error_message or "Не удалось выполнить запрос."), []
        # Still running (e.g. a heavier business workflow) -- realtime voice
        # never blocks indefinitely nor fabricates completion.
        return "Запрос принят в обработку.", []

    async def _run_turn(self, session: RealtimeSession, *, turn_id: str, text: str, turn_start: float) -> None:
        try:
            # Stage A is shielded: barge-in may stop US from waiting on it,
            # but must never cancel an already-dispatched tool/business
            # action mid-flight (Block 4.13/4.23 "no accidental repeated
            # external action" + "preserve the correct textual record").
            reply_text, artifacts = await asyncio.shield(self._submit(session, turn_id=turn_id, text=text))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            REALTIME_METRICS.inc_error("conversation_unavailable")
            await session.sink.send_event(
                session.events.build(
                    EV_ERROR, turn_id=turn_id, code=RT_CONVERSATION_UNAVAILABLE, message=redact(str(exc))
                )
            )
            if session.state_machine.can(SessionEvent.ERROR):
                session.state_machine.transition(SessionEvent.ERROR)
            return

        if artifacts:
            status = _status_for_artifacts(artifacts)
            await session.sink.send_event(session.events.build(EV_TOOL_STARTED, turn_id=turn_id))
            await session.sink.send_event(session.events.build(EV_ASSISTANT_STATUS, turn_id=turn_id, status=status))
            await session.sink.send_event(
                session.events.build(EV_TOOL_COMPLETED, turn_id=turn_id, artifact_count=len(artifacts))
            )
            REALTIME_METRICS.inc("tool_dispatched")

        await self._stream_text_and_audio(session, turn_id=turn_id, text=reply_text, turn_start=turn_start)

    async def _stream_text_and_audio(
        self, session: RealtimeSession, *, turn_id: str, text: str, turn_start: float
    ) -> None:
        if session.state_machine.can(SessionEvent.RESPONSE_STARTED):
            session.state_machine.transition(SessionEvent.RESPONSE_STARTED)

        first_text = False
        for chunk in _chunk_text(text) or [""]:
            await session.sink.send_event(
                session.events.build(EV_ASSISTANT_TEXT_DELTA, turn_id=turn_id, delta=chunk)
            )
            if session.state_machine.can(SessionEvent.TEXT_STREAMING):
                session.state_machine.transition(SessionEvent.TEXT_STREAMING)
            if not first_text:
                REALTIME_METRICS.turn_to_first_text.observe((time.monotonic() - turn_start) * 1000)
                first_text = True
            await asyncio.sleep(0)
        await session.sink.send_event(session.events.build(EV_ASSISTANT_TEXT_COMPLETED, turn_id=turn_id, text=text))

        # One canonical Panda response feeds BOTH the visible/streamed text
        # above AND the TTS call below (production voice defect closure
        # section 10) -- `text` here is the exact same `reply_text` returned
        # by the SINGLE _submit()/submit_async() call in _run_turn, never a
        # second model request or an independently generated spoken answer.
        tts_started = time.monotonic()
        _log_voice_event(
            "tts_request_started", session=session, turn_id=turn_id, provider=type(self.tts).__name__, text_chars=len(text)
        )
        try:
            audio_bytes = self.tts.synthesize(text=text, voice=session.voice_id, mime_type="audio/mpeg")
            REALTIME_METRICS.inc("tts_call")
            self._record_speech_usage(session, capability="tts", turn_id=turn_id)
            _log_voice_event(
                "tts_request_completed",
                session=session,
                turn_id=turn_id,
                provider=type(self.tts).__name__,
                success=True,
                latency_ms=round((time.monotonic() - tts_started) * 1000, 1),
                audio_bytes=len(audio_bytes),
            )
        except Exception as exc:
            REALTIME_METRICS.inc_error("tts_failed")
            _log_voice_event(
                "tts_request_completed",
                session=session,
                turn_id=turn_id,
                provider=type(self.tts).__name__,
                success=False,
                latency_ms=round((time.monotonic() - tts_started) * 1000, 1),
                error_type=type(exc).__name__,
            )
            await session.sink.send_event(
                session.events.build(EV_ERROR, turn_id=turn_id, code=RT_TTS_FAILED, message="tts_failed")
            )
            if session.state_machine.can(SessionEvent.RESPONSE_COMPLETED):
                session.state_machine.transition(SessionEvent.RESPONSE_COMPLETED)
            return
        _log_voice_event("tts_audio_received", session=session, turn_id=turn_id, audio_bytes=len(audio_bytes))

        first_audio = False
        for idx, chunk in enumerate(_chunk_bytes(audio_bytes)):
            await session.sink.send_event(
                session.events.build(
                    EV_ASSISTANT_AUDIO_DELTA, turn_id=turn_id, chunk_index=idx, byte_size=len(chunk)
                )
            )
            await session.sink.send_audio(chunk, turn_id=turn_id)
            if session.state_machine.can(SessionEvent.AUDIO_STREAMING):
                session.state_machine.transition(SessionEvent.AUDIO_STREAMING)
            if not first_audio:
                REALTIME_METRICS.turn_to_first_audio.observe((time.monotonic() - turn_start) * 1000)
                first_audio = True
            await asyncio.sleep(0)
        await session.sink.send_event(
            session.events.build(EV_ASSISTANT_AUDIO_COMPLETED, turn_id=turn_id, total_bytes=len(audio_bytes))
        )
        if session.state_machine.can(SessionEvent.RESPONSE_COMPLETED):
            session.state_machine.transition(SessionEvent.RESPONSE_COMPLETED)
        # Continuous ChatGPT-style loop (section 12): the client
        # (static/panda/js/realtime.js _resumeListening(), triggered by the
        # assistant.audio.completed event above) automatically re-arms the
        # microphone for the next turn without any user action -- logged
        # here as the server-side half of that contract for diagnosis.
        _log_voice_event("voice_returned_to_listening", session=session, turn_id=turn_id)

    # --- barge-in / interruption --------------------------------------------

    async def barge_in(self, session: RealtimeSession) -> bool:
        task = session.current_task
        if task is None or task.done():
            return False
        if session.state_machine.state not in _INTERRUPTIBLE_STATES:
            return False
        t0 = time.monotonic()
        interrupted_turn = session.current_turn_id
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        if session.state_machine.can(SessionEvent.INTERRUPTION):
            session.state_machine.transition(SessionEvent.INTERRUPTION)
        await session.sink.send_event(session.events.build(EV_INTERRUPTION, turn_id=interrupted_turn))
        REALTIME_METRICS.inc("interruption")
        REALTIME_METRICS.interrupt_to_audio_stop.observe((time.monotonic() - t0) * 1000)
        return True

    # --- spoken approval/reject/cancel (Block 4.33) -------------------------

    async def _pending_approval(self, session: RealtimeSession) -> Any:
        try:
            msgs = self.ba_api.get_conversation_messages(
                tenant_id=session.tenant_id, owner_id=session.owner_id, conversation_id=session.conversation_id
            )
        except Exception:
            return None
        seen: set[str] = set()
        pending = []
        for m in reversed(msgs):
            rid = m.get("request_id") if isinstance(m, dict) else getattr(m, "request_id", "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            try:
                rec = self.ba_api.get_request(
                    tenant_id=session.tenant_id, owner_id=session.owner_id, request_id=rid
                )
            except Exception:
                continue
            if rec.status == ST_WAITING_FOR_APPROVAL:
                pending.append(rec)
        return pending[0] if len(pending) == 1 else None

    async def _handle_spoken_confirmation(self, session: RealtimeSession, transcript: str) -> str | None:
        pending = await self._pending_approval(session)
        if pending is None:
            # No unambiguous pending approval to target -- never guess; treat
            # as an ordinary conversational turn instead (same as text chat).
            return await self.commit_turn(session, text=transcript, is_voice=True)

        action = "approve" if is_explicit_approval_intent(transcript) else (
            "reject" if is_reject_intent(transcript) else "cancel"
        )
        turn_id = session.next_turn_id()
        session.committed_turn_ids.add(turn_id)
        await session.sink.send_event(
            session.events.build(EV_USER_TURN_COMMITTED, turn_id=turn_id, text=transcript, modality="voice")
        )
        if session.state_machine.can(SessionEvent.TURN_COMMITTED):
            session.state_machine.transition(SessionEvent.TURN_COMMITTED)
        try:
            # Reuses the EXACT same HITL boundary the text "Approve"/"Reject"/
            # "Cancel" UI buttons call -- spoken confirmation never bypasses
            # authorization/HITL (Block 4.33).
            if action == "approve":
                self.ba_api.approve(
                    tenant_id=session.tenant_id, owner_id=session.owner_id, request_id=pending.request_id
                )
            elif action == "reject":
                self.ba_api.reject(
                    tenant_id=session.tenant_id, owner_id=session.owner_id, request_id=pending.request_id
                )
            else:
                self.ba_api.cancel(
                    tenant_id=session.tenant_id, owner_id=session.owner_id, request_id=pending.request_id
                )
        except Exception as exc:
            await session.sink.send_event(
                session.events.build(
                    EV_ERROR, turn_id=turn_id, code=RT_CONVERSATION_UNAVAILABLE, message=redact(str(exc))
                )
            )
            if session.state_machine.can(SessionEvent.ERROR):
                session.state_machine.transition(SessionEvent.ERROR)
            return turn_id
        reply = {"approve": "Подтверждено.", "reject": "Отклонено.", "cancel": "Отменено."}[action]
        task = asyncio.create_task(
            self._stream_text_and_audio(session, turn_id=turn_id, text=reply, turn_start=time.monotonic())
        )
        session.current_task = task
        session.current_turn_id = turn_id
        return turn_id
