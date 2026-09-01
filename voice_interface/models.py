"""Voice interface contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VoiceAudioInput:
    content: bytes
    mime_type: str
    filename: str = "audio.wav"
    language_hint: str = "auto"
    duration_seconds: float | None = None


@dataclass
class SttResult:
    transcript: str
    language: str = "auto"
    duration_seconds: float | None = None
    provider_status: str = "ok"


@dataclass
class TtsResult:
    artifact_id: str
    mime_type: str
    byte_size: int
    provider_status: str = "ok"


@dataclass
class VoiceRequestRecord:
    voice_request_id: str
    tenant_id: str
    owner_id: str
    ba_request_id: str
    conversation_id: str
    transcript: str
    idempotency_key: str = ""
    tts_artifact_id: str = ""
    tts_error: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
