"""Production STT/TTS adapters."""

from __future__ import annotations

import io
import time
from dataclasses import dataclass

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient
from integrations.production.observability import ProviderObservability
from ui_chat.voice.stt import FakeSpeechToTextProvider, SpeechToTextProvider
from ui_chat.voice.tts import FakeTextToSpeechProvider, TextToSpeechProvider


def _stt_filename_for_mime(mime_type: str) -> str:
    """Boundary F (production voice defect closure): the actual bytes
    MediaRecorder produces in the browser are webm/opus or ogg/opus, NEVER
    wav -- OpenAI's transcription endpoint selects its demuxer primarily
    from the uploaded filename's extension, so sending real webm/opus bytes
    under a hardcoded "audio.wav" name is a real, silent container/codec
    mismatch that can corrupt or reject production transcription even once
    a real provider + real credentials are configured. Map the ACTUAL
    negotiated mime type (realtime/session.py RealtimeSession.mime_type,
    set from the client's MediaRecorder.isTypeSupported negotiation) to a
    matching filename/extension instead of assuming wav."""

    mime = str(mime_type or "").lower()
    if "webm" in mime:
        return "audio.webm"
    if "ogg" in mime:
        return "audio.ogg"
    if "mp4" in mime or "m4a" in mime:
        return "audio.mp4"
    if "mpeg" in mime or "mp3" in mime:
        return "audio.mp3"
    return "audio.wav"


@dataclass
class OpenAISpeechToTextProvider:
    api_key: str
    model: str = "whisper-1"
    timeout_seconds: float = 60.0
    max_audio_bytes: int = 25 * 1024 * 1024
    obs: ProviderObservability | None = None
    _http: BoundedHttpClient | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ProductionProviderError(ProviderErrorCategory.CONFIGURATION_ERROR, message="stt_key_missing", provider_id="speech_stt")
        self._http = BoundedHttpClient(provider_id="speech_stt", timeout_seconds=self.timeout_seconds, max_response_bytes=512_000)

    def transcribe(self, *, audio: bytes, mime_type: str, language: str = "auto") -> str:
        if not audio:
            raise ValueError("empty_audio")
        if len(audio) > self.max_audio_bytes:
            raise ValueError("audio_too_large")
        started = time.monotonic()
        filename = _stt_filename_for_mime(mime_type)
        files = {"file": (filename, audio, mime_type or "audio/wav")}
        data = {"model": self.model}
        if language != "auto":
            data["language"] = language
        resp = self._http._get_client().post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data=data,
            files=files,
            timeout=self.timeout_seconds,
        )
        if resp.status_code >= 400:
            raise ValueError("stt_failed")
        payload = resp.json()
        text = str(payload.get("text") or "").strip()
        if self.obs:
            self.obs.emit(provider_id="speech_stt", operation="transcribe", success=bool(text), latency_ms=(time.monotonic() - started) * 1000)
        return text

    def health_check(self) -> dict:
        return {"status": "configured", "model": self.model}


@dataclass
class OpenAITextToSpeechProvider:
    api_key: str
    model: str = "tts-1"
    timeout_seconds: float = 60.0
    max_chars: int = 4096
    obs: ProviderObservability | None = None
    _http: BoundedHttpClient | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ProductionProviderError(ProviderErrorCategory.CONFIGURATION_ERROR, message="tts_key_missing", provider_id="speech_tts")
        self._http = BoundedHttpClient(provider_id="speech_tts", timeout_seconds=self.timeout_seconds, max_response_bytes=8_000_000)

    def synthesize(self, *, text: str, voice: str = "alloy", mime_type: str = "audio/mpeg") -> bytes:
        if len(text) > self.max_chars:
            raise ValueError("text_too_long")
        if not text.strip():
            raise ValueError("empty_text")
        started = time.monotonic()
        resp = self._http.request(
            "POST",
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json_body={"model": self.model, "input": text[: self.max_chars], "voice": voice},
        )
        if self.obs:
            self.obs.emit(provider_id="speech_tts", operation="synthesize", success=True, latency_ms=(time.monotonic() - started) * 1000)
        return resp.content

    def health_check(self) -> dict:
        return {"status": "configured", "model": self.model}


def _flag(env: dict, name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _voice_feature_active(env: dict) -> bool:
    """Root-cause fix (production voice defect closure): the ORIGINAL
    fail-closed check below only fired on the legacy `UI_CHAT_VOICE_ENABLED`
    flag, which no voice-consuming feature actually sets or reads --
    `realtime.runtime.realtime_enabled()` (Block 4 realtime voice, default
    ON) and `voice_interface.config.voice_interface_enabled()` (legacy
    record/upload voice, default ON) are the flags that are actually
    checked before a real user request reaches this module. That mismatch
    is the exact reason production could silently construct
    FakeSpeechToTextProvider/FakeTextToSpeechProvider (see ui_chat/voice/
    stt.py's "Transcribed voice input." literal and ui_chat/voice/tts.py's
    non-audio placeholder bytes) for a real, live voice session with zero
    startup failure. Duplicated here (not imported) to avoid a circular
    import (realtime.runtime already imports build_speech_providers)."""

    return (
        _flag(env, "REALTIME_ENABLED", True)
        or _flag(env, "VOICE_INTERFACE_ENABLED", True)
        or _flag(env, "UI_CHAT_VOICE_ENABLED", False)
    )


def speech_provider_diagnostics(env: dict) -> dict:
    """Safe (no keys/secrets) production readiness diagnostics for the
    admin ops provider matrix (integrations/production/factory.py) --
    section 5/9 of the production voice defect closure: configured/not,
    selected provider, model, real vs fake, ready/unavailable."""

    provider = str(env.get("SPEECH_PROVIDER") or "fake").strip().lower()
    key = str(env.get("SPEECH_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    is_real = provider != "fake" and bool(key)
    return {
        "provider_kind": "real" if is_real else "fake",
        "selected_provider": "openai" if is_real else "fake",
        "stt_model": str(env.get("SPEECH_STT_MODEL") or "whisper-1") if is_real else "",
        "tts_model": str(env.get("SPEECH_TTS_MODEL") or "tts-1") if is_real else "",
        "ready": is_real,
        "voice_feature_active": _voice_feature_active(env),
    }


def build_speech_providers(env: dict) -> tuple[SpeechToTextProvider, TextToSpeechProvider]:
    provider = str(env.get("SPEECH_PROVIDER") or "fake").strip().lower()
    key = str(env.get("SPEECH_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    is_real = provider != "fake" and bool(key)
    prod = str(env.get("PANDA_ENV") or env.get("ENVIRONMENT") or "").strip().lower() in {"production", "prod"}

    if not is_real:
        # Production fail-closed guard (root cause of the "Transcribed voice
        # input." / silent-Panda production defects): a live voice-capable
        # deployment MUST NOT silently resolve to Fake STT/TTS. Only a
        # deployment that has explicitly disabled EVERY voice-consuming
        # feature may run production without real speech credentials.
        if prod and _voice_feature_active(env):
            raise ProductionProviderError(
                ProviderErrorCategory.CONFIGURATION_ERROR,
                message="speech_key_required",
                provider_id="speech",
            )
        return FakeSpeechToTextProvider(), FakeTextToSpeechProvider()
    stt = OpenAISpeechToTextProvider(api_key=key, model=str(env.get("SPEECH_STT_MODEL") or "whisper-1"))
    tts = OpenAITextToSpeechProvider(api_key=key, model=str(env.get("SPEECH_TTS_MODEL") or "tts-1"))
    return stt, tts
