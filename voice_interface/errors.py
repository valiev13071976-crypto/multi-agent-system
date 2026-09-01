"""Voice interface errors."""

from __future__ import annotations


class VoiceInterfaceError(Exception):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400, retryable: bool = False):
        self.code = code
        self.message = message or code
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(self.message)


VI_AUDIO_EMPTY = "vi_audio_empty"
VI_AUDIO_TOO_LARGE = "vi_audio_too_large"
VI_AUDIO_UNSUPPORTED = "vi_audio_unsupported"
VI_AUDIO_MALFORMED = "vi_audio_malformed"
VI_STT_FAILED = "vi_stt_failed"
VI_STT_TIMEOUT = "vi_stt_timeout"
VI_STT_EMPTY = "vi_stt_empty_transcript"
VI_TTS_FAILED = "vi_tts_failed"
VI_TTS_UNAVAILABLE = "vi_tts_unavailable"
VI_ACCESS_DENIED = "vi_access_denied"
VI_AMBIGUOUS_APPROVAL = "vi_ambiguous_spoken_approval"
VI_NOT_FOUND = "vi_not_found"
VI_APPROVAL_NOT_PENDING = "vi_approval_not_pending"
