"""Personalization errors."""

from __future__ import annotations


class PersonalizationError(Exception):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400):
        self.code = code
        self.message = message or code
        self.http_status = http_status
        super().__init__(self.message)


PZ_INVALID_STYLE = "pz_invalid_style"
PZ_INVALID_TONE = "pz_invalid_tone"
PZ_INVALID_LENGTH = "pz_invalid_length"
PZ_INVALID_LANGUAGE = "pz_invalid_language"
PZ_INVALID_VOICE = "pz_invalid_voice"
PZ_PREVIEW_FAILED = "pz_preview_failed"
