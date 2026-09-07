"""Realtime session — server-side state for one connected realtime
transport (WebSocket) instance."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from realtime.events import RealtimeEvent, RealtimeEventFactory
from realtime.state_machine import RealtimeStateMachine


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RealtimeSink(Protocol):
    """Transport-facing output boundary. The bridge never touches a raw
    WebSocket -- router.py implements this Protocol."""

    async def send_event(self, event: RealtimeEvent) -> None: ...
    async def send_audio(self, chunk: bytes, *, turn_id: str) -> None: ...


@dataclass
class NullSink:
    """Test/offline sink -- records everything sent, sends nothing over
    the network. Used directly by unit tests of the bridge."""

    events: list[RealtimeEvent] = field(default_factory=list)
    audio_chunks: list[tuple[str, bytes]] = field(default_factory=list)

    async def send_event(self, event: RealtimeEvent) -> None:
        self.events.append(event)

    async def send_audio(self, chunk: bytes, *, turn_id: str) -> None:
        self.audio_chunks.append((turn_id, chunk))


@dataclass
class RealtimeSession:
    session_id: str
    tenant_id: str
    owner_id: str
    conversation_id: str
    voice_id: str
    sink: RealtimeSink
    state_machine: RealtimeStateMachine = field(default_factory=RealtimeStateMachine)
    events: RealtimeEventFactory | None = None
    language_hint: str = "ru"
    # Production voice defect closure Boundary F: the ACTUAL container/codec
    # mime type MediaRecorder negotiated in the browser (e.g.
    # "audio/webm;codecs=opus") -- never assume "audio/wav" server-side, a
    # real STT provider is sensitive to filename/content-type mismatches.
    mime_type: str = "audio/webm"
    created_at: str = field(default_factory=_utc_iso)
    audio_buffer: bytearray = field(default_factory=bytearray)
    last_partial_transcript: str = ""
    committed_turn_ids: set[str] = field(default_factory=set)
    turn_counter: int = 0
    current_task: "asyncio.Task | None" = None
    current_turn_id: str = ""
    turn_started_monotonic: float | None = None
    first_transcript_recorded: bool = False
    closed: bool = False
    # DEFECT B latency acceptance: throttle for the buffer-based partial-STT
    # re-transcription in on_audio_chunk (see bridge.py _PARTIAL_STT_MIN_
    # INTERVAL_SECONDS) -- without this, a real STT provider call on EVERY
    # ~250ms MediaRecorder chunk serializes into a backlog on this
    # connection's single WebSocket receive loop (router.py's `while True:
    # await websocket.receive()`), so audio.commit is only READ after that
    # backlog drains -- the exact "user stops talking -> long silent wait"
    # symptom. None until the first partial call of a turn.
    last_partial_stt_monotonic: float | None = None
    # PRODUCTION ACCEPTANCE FAILED follow-up: True once this uncommitted
    # capture has exceeded _PARTIAL_STT_MAX_UNCOMMITTED_SECONDS and further
    # partial-STT calls have been suppressed -- logged once (not every
    # chunk) via voice_capture_partial_stt_capped. Reset on every new
    # capture's first chunk.
    partial_stt_capped_logged: bool = False
    # DEFECT B latency acceptance: one monotonic timestamp per named stage
    # (speech_start, speech_end, stt_partial, stt_final, turn_committed,
    # assistant_processing_started, first_text_delta, tts_started,
    # first_audio_chunk, browser_playback_started, assistant_completed,
    # listening_resumed) for the turn currently in flight -- reset at the
    # start of each new turn's audio/text capture. First occurrence per
    # stage wins (never overwritten), so throttled/repeated calls (e.g.
    # multiple stt_partial events) never distort the timeline.
    turn_latency_marks: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = RealtimeEventFactory(
                session_id=self.session_id, conversation_id=self.conversation_id
            )

    def next_turn_id(self) -> str:
        self.turn_counter += 1
        return f"turn_{self.session_id[:8]}_{self.turn_counter}"


def new_session_id() -> str:
    return f"rts_{uuid.uuid4().hex[:16]}"
