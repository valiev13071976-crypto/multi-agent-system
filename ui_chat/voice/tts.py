"""Deterministic fake Text-to-Speech provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class TextToSpeechProvider(Protocol):
    def synthesize(self, *, text: str, voice: str = "default", mime_type: str = "audio/wav") -> bytes: ...


@dataclass
class FakeTextToSpeechProvider:
    max_chars: int = 8000

    def synthesize(self, *, text: str, voice: str = "default", mime_type: str = "audio/wav") -> bytes:
        if len(text) > self.max_chars:
            raise ValueError("text_too_long")
        if not text.strip():
            raise ValueError("empty_text")
        header = b"RIFF" + b"\x00" * 4 + b"WAVEfmt "
        payload = f"TTS:{voice}:{text[:120]}".encode("utf-8")
        return header + payload
