"""Block 4.25 — bounded-cardinality realtime session observability counters
+ latency aggregates (Block 4.24). Follows the exact process-local counter
pattern already used by artifacts/metrics.py and runtime/metrics.py.

Never keyed by session_id / conversation_id / turn_id / raw tenant_id /
transcript content / raw audio -- only by the small, fixed label sets below.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from observability.runtime_metrics import LatencyAgg

EVENT_NAMES = (
    "session_started",
    "session_ended",
    "session_reconnected",
    "interruption",
    "stt_call",
    "tts_call",
    "tool_dispatched",
    "voice_preview",
)

ERROR_CATEGORIES = (
    "auth_failed",
    "stt_failed",
    "tts_failed",
    "conversation_unavailable",
    "invalid_frame",
    "transport_disconnect",
    "unknown",
)


@dataclass
class RealtimeMetricsRegistry:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counts: dict[str, int] = field(default_factory=dict)
    _errors_by_category: dict[str, int] = field(default_factory=dict)

    mic_to_first_transcript: LatencyAgg = field(default_factory=LatencyAgg)
    turn_to_first_text: LatencyAgg = field(default_factory=LatencyAgg)
    turn_to_first_audio: LatencyAgg = field(default_factory=LatencyAgg)
    interrupt_to_audio_stop: LatencyAgg = field(default_factory=LatencyAgg)
    reconnect_duration: LatencyAgg = field(default_factory=LatencyAgg)

    def inc(self, event: str) -> None:
        name = str(event or "").strip()
        if name not in EVENT_NAMES:
            return
        with self._lock:
            self._counts[name] = int(self._counts.get(name, 0)) + 1

    def inc_error(self, category: str) -> None:
        cat = category if category in ERROR_CATEGORIES else "unknown"
        with self._lock:
            self._errors_by_category[cat] = int(self._errors_by_category.get(cat, 0)) + 1

    def as_dict(self) -> dict:
        with self._lock:
            counts = dict(self._counts)
            errors = dict(self._errors_by_category)
        return {
            "counts": counts,
            "errors_by_category": errors,
            "latency_ms": {
                "mic_to_first_transcript": self.mic_to_first_transcript.as_dict(),
                "turn_to_first_text": self.turn_to_first_text.as_dict(),
                "turn_to_first_audio": self.turn_to_first_audio.as_dict(),
                "interrupt_to_audio_stop": self.interrupt_to_audio_stop.as_dict(),
                "reconnect_duration": self.reconnect_duration.as_dict(),
            },
        }

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._errors_by_category.clear()
        self.mic_to_first_transcript = LatencyAgg()
        self.turn_to_first_text = LatencyAgg()
        self.turn_to_first_audio = LatencyAgg()
        self.interrupt_to_audio_stop = LatencyAgg()
        self.reconnect_duration = LatencyAgg()


REALTIME_METRICS = RealtimeMetricsRegistry()
