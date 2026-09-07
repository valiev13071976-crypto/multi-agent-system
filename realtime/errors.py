"""Realtime session errors."""

from __future__ import annotations


class RealtimeError(Exception):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400, retryable: bool = False):
        self.code = code
        self.message = message or code
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(self.message)


RT_AUTH_FAILED = "rt_auth_failed"
RT_SESSION_NOT_FOUND = "rt_session_not_found"
RT_INVALID_FRAME = "rt_invalid_frame"
RT_AUDIO_EMPTY = "rt_audio_empty"
RT_STT_FAILED = "rt_stt_failed"
RT_STT_EMPTY = "rt_stt_empty_transcript"
RT_TTS_FAILED = "rt_tts_failed"
RT_CONVERSATION_UNAVAILABLE = "rt_conversation_unavailable"
RT_TURN_ALREADY_COMMITTED = "rt_turn_already_committed"
