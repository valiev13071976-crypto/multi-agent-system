"""UI Chat errors — safe client-facing codes."""

from __future__ import annotations


class UIChatError(Exception):
    def __init__(self, code: str, *, message: str = "", retryable: bool = False):
        self.code = code
        self.message = message or code
        self.retryable = retryable
        super().__init__(self.code)


CHAT_NOT_FOUND = "chat_not_found"
CHAT_ACCESS_DENIED = "chat_access_denied"
CHAT_EMPTY_TURN = "chat_empty_turn"
CHAT_IDEMPOTENCY_CONFLICT = "chat_idempotency_conflict"
CHAT_RUN_NOT_FOUND = "chat_run_not_found"
CHAT_RUN_NOT_CANCELLABLE = "chat_run_not_cancellable"
ATTACHMENT_NOT_FOUND = "attachment_not_found"
ATTACHMENT_TOO_LARGE = "attachment_too_large"
ATTACHMENT_UNSUPPORTED = "attachment_unsupported"
ATTACHMENT_COUNT_EXCEEDED = "attachment_count_exceeded"
ATTACHMENT_AGGREGATE_TOO_LARGE = "attachment_aggregate_too_large"
VOICE_AUDIO_EMPTY = "voice_audio_empty"
VOICE_AUDIO_TOO_LARGE = "voice_audio_too_large"
VOICE_AUDIO_UNSUPPORTED = "voice_audio_unsupported"
VOICE_TRANSCRIPTION_FAILED = "voice_transcription_failed"
VOICE_TTS_FAILED = "voice_tts_failed"
VOICE_TTS_TOO_LONG = "voice_tts_too_long"
VOICE_MESSAGE_NOT_FOUND = "voice_message_not_found"
VOICE_AUDIO_NOT_FOUND = "voice_audio_not_found"
TASK_NOT_FOUND = "task_not_found"
TASK_NOT_CANCELLABLE = "task_not_cancellable"
