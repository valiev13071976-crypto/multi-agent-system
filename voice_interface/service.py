"""Voice interface orchestration — Business Assistant API transport only."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from business_assistant_api.errors import BusinessAssistantApiError
from business_assistant_api.models import ST_COMPLETED, ST_WAITING_FOR_APPROVAL, TERMINAL_STATES
from business_assistant_api.service import BusinessAssistantApiService
from integrations.production.adapters.speech import build_speech_providers
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
from voice_interface.audio import validate_audio
from voice_interface.config import tts_enabled, voice_audio_dir
from voice_interface.errors import (
    VI_AMBIGUOUS_APPROVAL,
    VI_APPROVAL_NOT_PENDING,
    VI_NOT_FOUND,
    VI_STT_EMPTY,
    VI_STT_FAILED,
    VI_TTS_FAILED,
    VoiceInterfaceError,
)
from voice_interface.models import SttResult, TtsResult, VoiceAudioInput, VoiceRequestRecord
from voice_interface.store import SqliteVoiceInterfaceStore


_TERMINAL = frozenset(TERMINAL_STATES)
_TTS_MAX_CHARS = 2000


class VoiceInterfaceService:
    def __init__(
        self,
        *,
        store: SqliteVoiceInterfaceStore,
        ba_api: BusinessAssistantApiService,
        stt: SpeechToTextProvider | None = None,
        tts: TextToSpeechProvider | None = None,
        audio_dir: str = "",
        env: dict | None = None,
    ):
        self.store = store
        self.ba = ba_api
        self.env = dict(env or {})
        if stt is None or tts is None:
            built_stt, built_tts = build_speech_providers(self.env)
            self.stt = stt or built_stt
            self.tts = tts or built_tts
        else:
            self.stt = stt
            self.tts = tts
        self.audio_dir = audio_dir or voice_audio_dir(self.env)
        Path(self.audio_dir).mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.store.close()

    def transcribe(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        audio: VoiceAudioInput,
    ) -> SttResult:
        try:
            text = self.stt.transcribe(audio=audio.content, mime_type=audio.mime_type, language=audio.language_hint)
        except Exception as exc:
            raise VoiceInterfaceError(VI_STT_FAILED, redact(str(exc)), http_status=422, retryable=True) from exc
        text = normalize_transcript(text)
        if not text:
            raise VoiceInterfaceError(VI_STT_EMPTY, http_status=422)
        return SttResult(transcript=text, language=audio.language_hint, duration_seconds=audio.duration_seconds)

    def submit_voice_request(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        audio: VoiceAudioInput,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        artifact_refs: list[str] | None = None,
        read_only: bool = False,
    ) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        idem = (idempotency_key or "").strip()
        if idem:
            existing = self.store.get_by_idempotency(tenant_id=tenant, owner_id=owner_id, idempotency_key=idem)
            if existing:
                return self._response_from_record(existing)

        stt = self.transcribe(tenant_id=tenant, owner_id=owner_id, audio=audio)
        transcript = stt.transcript

        if is_explicit_approval_intent(transcript):
            return self._handle_spoken_approval(
                tenant_id=tenant, owner_id=owner_id, conversation_id=conversation_id, idempotency_key=idem
            )
        if is_reject_intent(transcript):
            return self._handle_spoken_action(
                tenant_id=tenant, owner_id=owner_id, conversation_id=conversation_id, action="reject"
            )
        if is_cancel_intent(transcript):
            return self._handle_spoken_action(
                tenant_id=tenant, owner_id=owner_id, conversation_id=conversation_id, action="cancel"
            )

        conv_id = conversation_id or self._ensure_conversation(tenant, owner_id)
        ba_idem = idem or f"voice-{uuid.uuid4().hex}"
        rec = self.ba.submit(
            tenant_id=tenant,
            owner_id=owner_id,
            message=transcript,
            conversation_id=conv_id,
            artifact_refs=artifact_refs or [],
            idempotency_key=ba_idem,
            read_only=read_only,
            trace_id=f"voice-{ba_idem}",
        )
        voice_rec = self.store.create_voice_request(
            tenant_id=tenant,
            owner_id=owner_id,
            ba_request_id=rec.request_id,
            conversation_id=conv_id,
            transcript=transcript,
            idempotency_key=idem,
        )
        return self._enrich_response(voice_rec, rec.status)

    def get_voice_request(self, *, tenant_id: str, owner_id: str, request_id: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        voice_rec = self.store.get_by_ba_request(tenant_id=tenant, owner_id=owner_id, ba_request_id=request_id)
        if voice_rec is None:
            rec = self.ba.get_request(tenant_id=tenant, owner_id=owner_id, request_id=request_id)
            voice_rec = VoiceRequestRecord(
                voice_request_id=f"vr_{request_id[:12]}",
                tenant_id=tenant,
                owner_id=owner_id,
                ba_request_id=rec.request_id,
                conversation_id=rec.conversation_id,
                transcript="",
                created_at=rec.created_at,
            )
        rec = self.ba.get_request(tenant_id=tenant, owner_id=owner_id, request_id=request_id)
        return self._enrich_response(voice_rec, rec.status)

    def approve(self, *, tenant_id: str, owner_id: str, request_id: str) -> dict[str, Any]:
        rec = self.ba.approve(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        voice_rec = self.store.get_by_ba_request(
            tenant_id=tenant_id, owner_id=owner_id, ba_request_id=request_id
        )
        if voice_rec:
            return self._enrich_response(voice_rec, rec.status)
        return {"request_id": rec.request_id, "status": rec.status}

    def reject(self, *, tenant_id: str, owner_id: str, request_id: str) -> dict[str, Any]:
        rec = self.ba.reject(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        voice_rec = self.store.get_by_ba_request(
            tenant_id=tenant_id, owner_id=owner_id, ba_request_id=request_id
        )
        if voice_rec:
            return self._enrich_response(voice_rec, rec.status)
        return {"request_id": rec.request_id, "status": rec.status}

    def cancel(self, *, tenant_id: str, owner_id: str, request_id: str) -> dict[str, Any]:
        rec = self.ba.cancel(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        voice_rec = self.store.get_by_ba_request(
            tenant_id=tenant_id, owner_id=owner_id, ba_request_id=request_id
        )
        if voice_rec:
            return self._enrich_response(voice_rec, rec.status)
        return {"request_id": rec.request_id, "status": rec.status}

    def get_tts_audio(self, *, tenant_id: str, owner_id: str, artifact_id: str) -> tuple[bytes, str]:
        meta = self.store.get_tts_artifact(artifact_id=artifact_id, tenant_id=tenant_id, owner_id=owner_id)
        if meta is None:
            raise VoiceInterfaceError(VI_NOT_FOUND, http_status=404)
        path = Path(meta["storage_path"])
        if not path.is_file():
            raise VoiceInterfaceError(VI_NOT_FOUND, http_status=404)
        return path.read_bytes(), meta["mime_type"]

    def _ensure_conversation(self, tenant_id: str, owner_id: str) -> str:
        conv = self.ba.create_conversation(tenant_id=tenant_id, owner_id=owner_id, title="Voice")
        return conv.conversation_id

    def _pending_approvals(self, *, tenant_id: str, owner_id: str, conversation_id: str | None) -> list:
        if not conversation_id:
            return []
        pending = []
        try:
            msgs = self.ba.get_conversation_messages(
                tenant_id=tenant_id, owner_id=owner_id, conversation_id=conversation_id
            )
        except BusinessAssistantApiError:
            return []
        seen: set[str] = set()
        for m in reversed(msgs):
            rid = m.get("request_id") if isinstance(m, dict) else getattr(m, "request_id", "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            try:
                rec = self.ba.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=rid)
            except BusinessAssistantApiError:
                continue
            if rec.status == ST_WAITING_FOR_APPROVAL:
                pending.append(rec)
        return pending

    def _handle_spoken_approval(
        self, *, tenant_id: str, owner_id: str, conversation_id: str | None, idempotency_key: str
    ) -> dict[str, Any]:
        pending = self._pending_approvals(
            tenant_id=tenant_id, owner_id=owner_id, conversation_id=conversation_id
        )
        if len(pending) != 1:
            raise VoiceInterfaceError(VI_AMBIGUOUS_APPROVAL, http_status=409)
        target = pending[0]
        rec = self.ba.approve(
            tenant_id=tenant_id,
            owner_id=owner_id,
            request_id=target.request_id,
            approval_id=target.approval_id,
            plan_fingerprint=target.plan_fingerprint,
        )
        voice_rec = self.store.get_by_ba_request(
            tenant_id=tenant_id, owner_id=owner_id, ba_request_id=target.request_id
        )
        if voice_rec is None:
            voice_rec = self.store.create_voice_request(
                tenant_id=tenant_id,
                owner_id=owner_id,
                ba_request_id=target.request_id,
                conversation_id=target.conversation_id,
                transcript="[spoken approval]",
                idempotency_key=idempotency_key,
            )
        return self._enrich_response(voice_rec, rec.status)

    def _handle_spoken_action(
        self, *, tenant_id: str, owner_id: str, conversation_id: str | None, action: str
    ) -> dict[str, Any]:
        pending = self._pending_approvals(
            tenant_id=tenant_id, owner_id=owner_id, conversation_id=conversation_id
        )
        if len(pending) != 1:
            raise VoiceInterfaceError(VI_AMBIGUOUS_APPROVAL, http_status=409)
        target = pending[0]
        if action == "reject":
            rec = self.ba.reject(tenant_id=tenant_id, owner_id=owner_id, request_id=target.request_id)
        else:
            rec = self.ba.cancel(tenant_id=tenant_id, owner_id=owner_id, request_id=target.request_id)
        voice_rec = self.store.get_by_ba_request(
            tenant_id=tenant_id, owner_id=owner_id, ba_request_id=target.request_id
        )
        if voice_rec is None:
            voice_rec = VoiceRequestRecord(
                voice_request_id=f"vr_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant_id,
                owner_id=owner_id,
                ba_request_id=target.request_id,
                conversation_id=target.conversation_id,
                transcript=f"[spoken {action}]",
                created_at=rec.updated_at,
            )
        return self._enrich_response(voice_rec, rec.status)

    def _enrich_response(self, voice_rec: VoiceRequestRecord, status: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "voice_request_id": voice_rec.voice_request_id,
            "request_id": voice_rec.ba_request_id,
            "conversation_id": voice_rec.conversation_id,
            "status": status,
            "transcript": voice_rec.transcript,
        }
        if status == ST_WAITING_FOR_APPROVAL:
            try:
                preview = self.ba.get_preview(
                    tenant_id=voice_rec.tenant_id,
                    owner_id=voice_rec.owner_id,
                    request_id=voice_rec.ba_request_id,
                )
                out["preview"] = {
                    "changes": (preview.get("changes") or [])[:10],
                    "warnings": (preview.get("warnings") or [])[:5],
                }
            except BusinessAssistantApiError:
                pass
            return out

        if status in _TERMINAL and status == ST_COMPLETED:
            try:
                result = self.ba.get_result(
                    tenant_id=voice_rec.tenant_id,
                    owner_id=voice_rec.owner_id,
                    request_id=voice_rec.ba_request_id,
                )
                text = str(result.get("summary") or "")
                out["text_result"] = text
                if tts_enabled(self.env) and text.strip():
                    tts_out = self._try_tts(voice_rec, text)
                    if tts_out:
                        out["tts_artifact_id"] = tts_out.artifact_id
                        out["tts_mime_type"] = tts_out.mime_type
                    elif voice_rec.tts_error:
                        out["tts_error"] = voice_rec.tts_error
            except BusinessAssistantApiError:
                pass
        return out

    def _try_tts(self, voice_rec: VoiceRequestRecord, text: str) -> TtsResult | None:
        bounded = text[:_TTS_MAX_CHARS]
        try:
            blob = self.tts.synthesize(text=bounded, mime_type="audio/wav")
        except Exception as exc:
            voice_rec.tts_error = VI_TTS_FAILED
            self.store.save_voice_request(voice_rec)
            return None
        artifact_id = f"vtts_{uuid.uuid4().hex[:12]}"
        tenant_dir = Path(self.audio_dir) / voice_rec.tenant_id
        tenant_dir.mkdir(parents=True, exist_ok=True)
        path = tenant_dir / f"{artifact_id}.wav"
        path.write_bytes(blob)
        self.store.save_tts_artifact(
            artifact_id=artifact_id,
            tenant_id=voice_rec.tenant_id,
            owner_id=voice_rec.owner_id,
            mime_type="audio/wav",
            byte_size=len(blob),
            storage_path=str(path),
        )
        voice_rec.tts_artifact_id = artifact_id
        self.store.save_voice_request(voice_rec)
        return TtsResult(artifact_id=artifact_id, mime_type="audio/wav", byte_size=len(blob))

    def _response_from_record(self, rec: VoiceRequestRecord) -> dict[str, Any]:
        ba = self.ba.get_request(tenant_id=rec.tenant_id, owner_id=rec.owner_id, request_id=rec.ba_request_id)
        return self._enrich_response(rec, ba.status)
