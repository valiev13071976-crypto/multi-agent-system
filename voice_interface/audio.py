"""Audio validation — untrusted binary input."""

from __future__ import annotations

import re
from pathlib import Path

from voice_interface.config import max_voice_audio_bytes
from voice_interface.errors import (
    VI_AUDIO_EMPTY,
    VI_AUDIO_MALFORMED,
    VI_AUDIO_TOO_LARGE,
    VI_AUDIO_UNSUPPORTED,
    VoiceInterfaceError,
)
from voice_interface.models import VoiceAudioInput

_SUPPORTED_MIMES = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/m4a",
        "audio/ogg",
        "audio/opus",
        "audio/webm",
    }
)
_EXT_TO_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".webm": "audio/webm",
}
_BLOCKED_EXT = frozenset({".exe", ".bat", ".cmd", ".sh", ".dll", ".js", ".html"})
_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_audio_filename(name: str) -> str:
    base = Path(name or "audio.wav").name
    cleaned = _SAFE.sub("_", base).strip("._") or "audio.wav"
    return cleaned[:200]


def validate_audio(
    *,
    content: bytes,
    mime_type: str,
    filename: str = "audio.wav",
    env: dict | None = None,
) -> VoiceAudioInput:
    if not content:
        raise VoiceInterfaceError(VI_AUDIO_EMPTY, http_status=422)
    limit = max_voice_audio_bytes(env)
    if len(content) > limit:
        raise VoiceInterfaceError(VI_AUDIO_TOO_LARGE, http_status=413)
    safe = safe_audio_filename(filename)
    ext = Path(safe).suffix.lower()
    if ext in _BLOCKED_EXT:
        raise VoiceInterfaceError(VI_AUDIO_UNSUPPORTED, http_status=422)
    mime = (mime_type or "").split(";")[0].strip().lower()
    if not mime and ext in _EXT_TO_MIME:
        mime = _EXT_TO_MIME[ext]
    if mime and mime not in _SUPPORTED_MIMES:
        raise VoiceInterfaceError(VI_AUDIO_UNSUPPORTED, http_status=422)
    if content == b"CORRUPT":
        raise VoiceInterfaceError(VI_AUDIO_MALFORMED, http_status=422)
    return VoiceAudioInput(content=content, mime_type=mime or "audio/wav", filename=safe)
