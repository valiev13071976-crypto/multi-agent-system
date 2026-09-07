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
