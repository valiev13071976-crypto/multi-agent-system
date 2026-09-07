"""Block 4.34 — ONE canonical realtime session state machine.

Every transport/provider-specific state must map into this model. No
competing state machine may be implemented elsewhere (e.g. directly in the
WebSocket router or in frontend JS) -- both reuse this module's
``SessionState``/``SessionEvent``/``TRANSITIONS`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


class SessionState:
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    LISTENING = "LISTENING"
    USER_SPEAKING = "USER_SPEAKING"
    TRANSCRIBING = "TRANSCRIBING"
    USER_TURN_COMMITTED = "USER_TURN_COMMITTED"
    THINKING = "THINKING"
    ASSISTANT_STREAMING_TEXT = "ASSISTANT_STREAMING_TEXT"
    ASSISTANT_SPEAKING = "ASSISTANT_SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"
    CLOSED = "CLOSED"


ALL_STATES = frozenset(
    {
        SessionState.IDLE,
        SessionState.CONNECTING,
        SessionState.LISTENING,
        SessionState.USER_SPEAKING,
        SessionState.TRANSCRIBING,
        SessionState.USER_TURN_COMMITTED,
        SessionState.THINKING,
        SessionState.ASSISTANT_STREAMING_TEXT,
        SessionState.ASSISTANT_SPEAKING,
        SessionState.INTERRUPTED,
        SessionState.RECONNECTING,
        SessionState.ERROR,
        SessionState.CLOSED,
    }
)


class SessionEvent:
    CONNECT = "connect"
    CONNECTED = "connected"
    START_LISTENING = "start_listening"
    AUDIO_STARTED = "audio_started"
    AUDIO_COMMITTED = "audio_committed"
    TRANSCRIPT_FINAL = "transcript_final"
    TURN_COMMITTED = "turn_committed"
    RESPONSE_STARTED = "response_started"
    TEXT_STREAMING = "text_streaming"
    AUDIO_STREAMING = "audio_streaming"
    RESPONSE_COMPLETED = "response_completed"
    INTERRUPTION = "interruption"
    DISCONNECTED = "disconnected"
    RECONNECT_ATTEMPT = "reconnect_attempt"
    RECONNECTED = "reconnected"
    ERROR = "error"
    CLOSE = "close"
    TEXT_MESSAGE = "text_message"  # user switched to keyboard text mid-session


class IllegalTransitionError(Exception):
    def __init__(self, state: str, event: str):
        self.state = state
        self.event = event
        super().__init__(f"illegal_transition: {state} + {event}")


# (current_state, event) -> next_state. Every legal path required by Block 4
# is represented (voice turn, text turn, barge-in, reconnect, error/recovery).
_S = SessionState
_E = SessionEvent
TRANSITIONS: dict[tuple[str, str], str] = {
    (_S.IDLE, _E.CONNECT): _S.CONNECTING,
    (_S.CONNECTING, _E.CONNECTED): _S.LISTENING,
    (_S.CONNECTING, _E.DISCONNECTED): _S.CLOSED,
    # Voice turn.
    (_S.LISTENING, _E.AUDIO_STARTED): _S.USER_SPEAKING,
    (_S.USER_SPEAKING, _E.AUDIO_STARTED): _S.USER_SPEAKING,  # more chunks, same turn
    (_S.USER_SPEAKING, _E.AUDIO_COMMITTED): _S.TRANSCRIBING,
    (_S.TRANSCRIBING, _E.TRANSCRIPT_FINAL): _S.USER_TURN_COMMITTED,
    (_S.USER_TURN_COMMITTED, _E.TURN_COMMITTED): _S.THINKING,
    # Text turn (Block 4.14: voice <-> text switching reuses the same states).
    (_S.LISTENING, _E.TEXT_MESSAGE): _S.USER_TURN_COMMITTED,
    (_S.THINKING, _E.RESPONSE_STARTED): _S.ASSISTANT_STREAMING_TEXT,
    (_S.ASSISTANT_STREAMING_TEXT, _E.TEXT_STREAMING): _S.ASSISTANT_STREAMING_TEXT,
    (_S.ASSISTANT_STREAMING_TEXT, _E.AUDIO_STREAMING): _S.ASSISTANT_SPEAKING,
    (_S.ASSISTANT_SPEAKING, _E.AUDIO_STREAMING): _S.ASSISTANT_SPEAKING,
    (_S.ASSISTANT_STREAMING_TEXT, _E.RESPONSE_COMPLETED): _S.LISTENING,
    (_S.ASSISTANT_SPEAKING, _E.RESPONSE_COMPLETED): _S.LISTENING,
    # Barge-in (Block 4.13): legal from any assistant-output state, and also
    # while still thinking (user re-interrupts before any output started).
    (_S.ASSISTANT_STREAMING_TEXT, _E.INTERRUPTION): _S.INTERRUPTED,
    (_S.ASSISTANT_SPEAKING, _E.INTERRUPTION): _S.INTERRUPTED,
    (_S.THINKING, _E.INTERRUPTION): _S.INTERRUPTED,
    (_S.INTERRUPTED, _E.AUDIO_STARTED): _S.USER_SPEAKING,
    (_S.INTERRUPTED, _E.TEXT_MESSAGE): _S.USER_TURN_COMMITTED,
    (_S.INTERRUPTED, _E.RESPONSE_COMPLETED): _S.LISTENING,
    # Reconnect (Block 4.23).
    (_S.LISTENING, _E.DISCONNECTED): _S.RECONNECTING,
    (_S.USER_SPEAKING, _E.DISCONNECTED): _S.RECONNECTING,
    (_S.TRANSCRIBING, _E.DISCONNECTED): _S.RECONNECTING,
    (_S.THINKING, _E.DISCONNECTED): _S.RECONNECTING,
    (_S.ASSISTANT_STREAMING_TEXT, _E.DISCONNECTED): _S.RECONNECTING,
    (_S.ASSISTANT_SPEAKING, _E.DISCONNECTED): _S.RECONNECTING,
    (_S.RECONNECTING, _E.RECONNECT_ATTEMPT): _S.RECONNECTING,
    (_S.RECONNECTING, _E.RECONNECTED): _S.LISTENING,
    (_S.RECONNECTING, _E.ERROR): _S.ERROR,
    (_S.RECONNECTING, _E.CLOSE): _S.CLOSED,
    # Error/recovery (Block 4.36): a recoverable per-turn failure (STT/TTS/
    # tool/provider) returns the user straight to LISTENING so they can
    # simply try again -- no infinite spinner, no dead session, and no need
    # to run the full reconnect handshake for a single failed turn.
    (_S.TRANSCRIBING, _E.ERROR): _S.LISTENING,
    (_S.THINKING, _E.ERROR): _S.LISTENING,
    (_S.ASSISTANT_STREAMING_TEXT, _E.ERROR): _S.LISTENING,
    (_S.ASSISTANT_SPEAKING, _E.ERROR): _S.LISTENING,
    (_S.ERROR, _E.RECONNECT_ATTEMPT): _S.RECONNECTING,
    (_S.ERROR, _E.CLOSE): _S.CLOSED,
}
# Explicit close is legal from any non-terminal state; a hard/fatal error
# (transport/auth/connect-time failures not covered above) lands in ERROR,
# which only recovers via an explicit reconnect attempt. setdefault() never
# overrides the specific, more forgiving recovery transitions defined above.
for _state in ALL_STATES:
    if _state != _S.CLOSED:
        TRANSITIONS.setdefault((_state, _E.CLOSE), _S.CLOSED)
    if _state not in {_S.CLOSED, _S.ERROR}:
        TRANSITIONS.setdefault((_state, _E.ERROR), _S.ERROR)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TransitionRecord:
    from_state: str
    event: str
    to_state: str
    at: str = field(default_factory=_utc_iso)


class RealtimeStateMachine:
    """One instance per realtime session. Fails closed on illegal transitions
    (never silently coerces state -- callers must be honest about lifecycle)."""

    def __init__(self, *, initial: str = SessionState.IDLE):
        if initial not in ALL_STATES:
            raise ValueError(f"unknown_initial_state:{initial}")
        self.state = initial
        self.history: list[TransitionRecord] = []

    def can(self, event: str) -> bool:
        return (self.state, event) in TRANSITIONS

    def transition(self, event: str) -> str:
        key = (self.state, event)
        if key not in TRANSITIONS:
            raise IllegalTransitionError(self.state, event)
        next_state = TRANSITIONS[key]
        self.history.append(TransitionRecord(from_state=self.state, event=event, to_state=next_state))
        self.state = next_state
        return next_state
