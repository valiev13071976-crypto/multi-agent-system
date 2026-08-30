"""Deterministic fake Speech-to-Text provider for tests and closure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SpeechToTextProvider(Protocol):
    def transcribe(self, *, audio: bytes, mime_type: str, language: str = "auto") -> str: ...


@dataclass
class FakeSpeechToTextProvider:
    """Maps known test payloads to transcripts; otherwise returns generic text."""

    fail_on_empty: bool = True
    fail_mime_prefixes: tuple[str, ...] = ("application/octet-stream-unknown",)

    def transcribe(self, *, audio: bytes, mime_type: str, language: str = "auto") -> str:
        if self.fail_on_empty and not audio:
            raise ValueError("empty_audio")
        mime = str(mime_type or "").lower()
        for prefix in self.fail_mime_prefixes:
            if mime.startswith(prefix):
                raise ValueError("unsupported_audio")
        marker = b"PANDA_STT_TEST:"
        if audio.startswith(marker):
            return audio[len(marker) :].decode("utf-8", errors="replace").strip()
        if audio == b"CORRUPT":
            raise ValueError("corrupt_audio")
        return "Transcribed voice input."
