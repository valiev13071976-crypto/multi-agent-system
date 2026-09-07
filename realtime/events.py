"""Block 4.35 — canonical realtime event contract.

Every event carries: a unique event_id, a session-scoped monotonic sequence
number (ordering/dedupe), the session/conversation/turn association, and a
UTC timestamp. Event `type` strings match the master spec's dotted names
exactly so they double as stable wire-protocol identifiers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

EV_SESSION_STARTED = "session.started"
EV_SESSION_CONNECTED = "session.connected"
EV_USER_AUDIO_STARTED = "user.audio.started"
EV_USER_TRANSCRIPT_PARTIAL = "user.transcript.partial"
EV_USER_TRANSCRIPT_FINAL = "user.transcript.final"
EV_USER_TURN_COMMITTED = "user.turn.committed"
EV_ASSISTANT_STATUS = "assistant.status"
EV_ASSISTANT_TEXT_DELTA = "assistant.text.delta"
EV_ASSISTANT_TEXT_COMPLETED = "assistant.text.completed"
EV_ASSISTANT_AUDIO_DELTA = "assistant.audio.delta"
EV_ASSISTANT_AUDIO_COMPLETED = "assistant.audio.completed"
EV_TOOL_STARTED = "tool.started"
EV_TOOL_COMPLETED = "tool.completed"
EV_INTERRUPTION = "interruption"
EV_SESSION_RECONNECTING = "session.reconnecting"
EV_SESSION_RECONNECTED = "session.reconnected"
EV_ERROR = "error"
EV_SESSION_CLOSED = "session.closed"

ALL_EVENT_TYPES = frozenset(
    {
        EV_SESSION_STARTED,
        EV_SESSION_CONNECTED,
        EV_USER_AUDIO_STARTED,
        EV_USER_TRANSCRIPT_PARTIAL,
        EV_USER_TRANSCRIPT_FINAL,
        EV_USER_TURN_COMMITTED,
        EV_ASSISTANT_STATUS,
        EV_ASSISTANT_TEXT_DELTA,
        EV_ASSISTANT_TEXT_COMPLETED,
        EV_ASSISTANT_AUDIO_DELTA,
        EV_ASSISTANT_AUDIO_COMPLETED,
        EV_TOOL_STARTED,
        EV_TOOL_COMPLETED,
        EV_INTERRUPTION,
        EV_SESSION_RECONNECTING,
        EV_SESSION_RECONNECTED,
        EV_ERROR,
        EV_SESSION_CLOSED,
    }
)

# Block 4.19: allowed user-facing status labels only -- never raw internal
# routing/agent/chain-of-thought text.
STATUS_LISTENING = "Слушаю"
STATUS_THINKING = "Думаю"
STATUS_SEARCHING = "Ищу информацию"
STATUS_ANALYZING_FILE = "Анализирую файл"
STATUS_GENERATING_IMAGE = "Создаю изображение"
STATUS_COMPARING = "Сравниваю варианты"
STATUS_PREPARING_ANSWER = "Готовлю ответ"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RealtimeEvent:
    event_id: str
    type: str
    session_id: str
    conversation_id: str
    seq: int
    ts: str
    turn_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "seq": self.seq,
            "ts": self.ts,
            "data": dict(self.data),
        }


class RealtimeEventFactory:
    """One instance per session -- owns the session's monotonic sequence
    counter so every emitted event is strictly, verifiably ordered."""

    def __init__(self, *, session_id: str, conversation_id: str):
        self.session_id = session_id
        self.conversation_id = conversation_id
        self._seq = 0

    def build(self, event_type: str, *, turn_id: str = "", **data: Any) -> RealtimeEvent:
        if event_type not in ALL_EVENT_TYPES:
            raise ValueError(f"unknown_event_type:{event_type}")
        self._seq += 1
        return RealtimeEvent(
            event_id=str(uuid.uuid4()),
            type=event_type,
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            seq=self._seq,
            ts=_utc_iso(),
            turn_id=turn_id,
            data=dict(data),
        )
