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
        files = {"file": ("audio.wav", audio, mime_type or "audio/wav")}
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


def build_speech_providers(env: dict) -> tuple[SpeechToTextProvider, TextToSpeechProvider]:
    provider = str(env.get("SPEECH_PROVIDER") or "fake").strip().lower()
    if provider == "fake":
        return FakeSpeechToTextProvider(), FakeTextToSpeechProvider()
    key = str(env.get("SPEECH_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    prod = str(env.get("PANDA_ENV") or env.get("ENVIRONMENT") or "").strip().lower() in {"production", "prod"}
    if not key:
        if prod and str(env.get("UI_CHAT_VOICE_ENABLED") or "").lower() in {"1", "true", "yes", "on"}:
            raise ProductionProviderError(ProviderErrorCategory.CONFIGURATION_ERROR, message="speech_key_required", provider_id="speech")
        return FakeSpeechToTextProvider(), FakeTextToSpeechProvider()
    stt = OpenAISpeechToTextProvider(api_key=key, model=str(env.get("SPEECH_STT_MODEL") or "whisper-1"))
    tts = OpenAITextToSpeechProvider(api_key=key, model=str(env.get("SPEECH_TTS_MODEL") or "tts-1"))
    return stt, tts
