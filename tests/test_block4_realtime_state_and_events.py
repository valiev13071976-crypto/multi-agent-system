"""Block 4.34/4.35 — canonical realtime session state machine + event
contract unit tests (no transport, no providers)."""

from __future__ import annotations

import unittest

from realtime.events import (
    ALL_EVENT_TYPES,
    EV_SESSION_STARTED,
    EV_USER_TRANSCRIPT_FINAL,
    RealtimeEventFactory,
)
from realtime.state_machine import (
    IllegalTransitionError,
    RealtimeStateMachine,
    SessionEvent,
    SessionState,
)


class StateMachineTests(unittest.TestCase):
    def test_initial_state_is_idle(self):
        sm = RealtimeStateMachine()
        self.assertEqual(sm.state, SessionState.IDLE)

    def test_full_voice_turn_happy_path(self):
        sm = RealtimeStateMachine()
        sm.transition(SessionEvent.CONNECT)
        sm.transition(SessionEvent.CONNECTED)
        self.assertEqual(sm.state, SessionState.LISTENING)
        sm.transition(SessionEvent.AUDIO_STARTED)
        self.assertEqual(sm.state, SessionState.USER_SPEAKING)
        sm.transition(SessionEvent.AUDIO_COMMITTED)
        self.assertEqual(sm.state, SessionState.TRANSCRIBING)
        sm.transition(SessionEvent.TRANSCRIPT_FINAL)
        self.assertEqual(sm.state, SessionState.USER_TURN_COMMITTED)
        sm.transition(SessionEvent.TURN_COMMITTED)
        self.assertEqual(sm.state, SessionState.THINKING)
        sm.transition(SessionEvent.RESPONSE_STARTED)
        self.assertEqual(sm.state, SessionState.ASSISTANT_STREAMING_TEXT)
        sm.transition(SessionEvent.AUDIO_STREAMING)
        self.assertEqual(sm.state, SessionState.ASSISTANT_SPEAKING)
        sm.transition(SessionEvent.RESPONSE_COMPLETED)
        self.assertEqual(sm.state, SessionState.LISTENING)

    def test_text_turn_happy_path_reuses_same_states(self):
        sm = RealtimeStateMachine(initial=SessionState.LISTENING)
        sm.transition(SessionEvent.TEXT_MESSAGE)
        self.assertEqual(sm.state, SessionState.USER_TURN_COMMITTED)
        sm.transition(SessionEvent.TURN_COMMITTED)
        sm.transition(SessionEvent.RESPONSE_STARTED)
        sm.transition(SessionEvent.RESPONSE_COMPLETED)
        self.assertEqual(sm.state, SessionState.LISTENING)

    def test_barge_in_from_assistant_speaking_returns_to_user_speaking(self):
        sm = RealtimeStateMachine(initial=SessionState.ASSISTANT_SPEAKING)
        sm.transition(SessionEvent.INTERRUPTION)
        self.assertEqual(sm.state, SessionState.INTERRUPTED)
        sm.transition(SessionEvent.AUDIO_STARTED)
        self.assertEqual(sm.state, SessionState.USER_SPEAKING)

    def test_barge_in_legal_while_thinking(self):
        sm = RealtimeStateMachine(initial=SessionState.THINKING)
        sm.transition(SessionEvent.INTERRUPTION)
        self.assertEqual(sm.state, SessionState.INTERRUPTED)

    def test_illegal_transition_raises_and_never_mutates_state(self):
        sm = RealtimeStateMachine(initial=SessionState.IDLE)
        with self.assertRaises(IllegalTransitionError):
            sm.transition(SessionEvent.AUDIO_STARTED)
        self.assertEqual(sm.state, SessionState.IDLE)

    def test_can_reports_legality_without_mutating(self):
        sm = RealtimeStateMachine(initial=SessionState.LISTENING)
        self.assertTrue(sm.can(SessionEvent.AUDIO_STARTED))
        self.assertFalse(sm.can(SessionEvent.RESPONSE_COMPLETED))
        self.assertEqual(sm.state, SessionState.LISTENING)

    def test_recoverable_stt_failure_returns_to_listening_not_hard_error(self):
        sm = RealtimeStateMachine(initial=SessionState.TRANSCRIBING)
        sm.transition(SessionEvent.ERROR)
        self.assertEqual(sm.state, SessionState.LISTENING)

    def test_fatal_connect_failure_lands_in_error_and_requires_reconnect(self):
        sm = RealtimeStateMachine(initial=SessionState.LISTENING)
        # A raw transport-level error not covered by a specific recovery rule
        # (e.g. mid-listen socket fault) lands in the hard ERROR state.
        sm.transition(SessionEvent.ERROR)
        self.assertEqual(sm.state, SessionState.ERROR)
        with self.assertRaises(IllegalTransitionError):
            sm.transition(SessionEvent.AUDIO_STARTED)
        sm.transition(SessionEvent.RECONNECT_ATTEMPT)
        self.assertEqual(sm.state, SessionState.RECONNECTING)
        sm.transition(SessionEvent.RECONNECTED)
        self.assertEqual(sm.state, SessionState.LISTENING)

    def test_reconnect_flow(self):
        sm = RealtimeStateMachine(initial=SessionState.ASSISTANT_STREAMING_TEXT)
        sm.transition(SessionEvent.DISCONNECTED)
        self.assertEqual(sm.state, SessionState.RECONNECTING)
        sm.transition(SessionEvent.RECONNECT_ATTEMPT)
        self.assertEqual(sm.state, SessionState.RECONNECTING)
        sm.transition(SessionEvent.RECONNECTED)
        self.assertEqual(sm.state, SessionState.LISTENING)

    def test_close_legal_from_any_non_terminal_state(self):
        for state in (
            SessionState.IDLE,
            SessionState.LISTENING,
            SessionState.USER_SPEAKING,
            SessionState.THINKING,
            SessionState.ASSISTANT_SPEAKING,
            SessionState.RECONNECTING,
            SessionState.ERROR,
        ):
            sm = RealtimeStateMachine(initial=state)
            sm.transition(SessionEvent.CLOSE)
            self.assertEqual(sm.state, SessionState.CLOSED)

    def test_closed_is_terminal(self):
        sm = RealtimeStateMachine(initial=SessionState.CLOSED)
        with self.assertRaises(IllegalTransitionError):
            sm.transition(SessionEvent.CONNECT)

    def test_history_records_every_transition_in_order(self):
        sm = RealtimeStateMachine()
        sm.transition(SessionEvent.CONNECT)
        sm.transition(SessionEvent.CONNECTED)
        self.assertEqual(len(sm.history), 2)
        self.assertEqual(sm.history[0].from_state, SessionState.IDLE)
        self.assertEqual(sm.history[0].to_state, SessionState.CONNECTING)
        self.assertEqual(sm.history[1].to_state, SessionState.LISTENING)


class EventFactoryTests(unittest.TestCase):
    def test_sequence_strictly_increasing_and_unique(self):
        factory = RealtimeEventFactory(session_id="s1", conversation_id="c1")
        events = [factory.build(EV_SESSION_STARTED) for _ in range(5)]
        seqs = [e.seq for e in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_event_ids_are_unique(self):
        factory = RealtimeEventFactory(session_id="s1", conversation_id="c1")
        ids = {factory.build(EV_SESSION_STARTED).event_id for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_event_carries_session_conversation_turn_association(self):
        factory = RealtimeEventFactory(session_id="s1", conversation_id="c1")
        ev = factory.build(EV_USER_TRANSCRIPT_FINAL, turn_id="turn_1", text="привет")
        self.assertEqual(ev.session_id, "s1")
        self.assertEqual(ev.conversation_id, "c1")
        self.assertEqual(ev.turn_id, "turn_1")
        self.assertEqual(ev.data["text"], "привет")

    def test_unknown_event_type_rejected(self):
        factory = RealtimeEventFactory(session_id="s1", conversation_id="c1")
        with self.assertRaises(ValueError):
            factory.build("not.a.real.event")

    def test_to_dict_is_json_shape_stable(self):
        factory = RealtimeEventFactory(session_id="s1", conversation_id="c1")
        ev = factory.build(EV_SESSION_STARTED, foo="bar")
        d = ev.to_dict()
        self.assertEqual(
            set(d.keys()), {"event_id", "type", "session_id", "conversation_id", "turn_id", "seq", "ts", "data"}
        )
        self.assertEqual(d["data"], {"foo": "bar"})

    def test_every_master_spec_event_name_present(self):
        expected = {
            "session.started",
            "session.connected",
            "user.audio.started",
            "user.transcript.partial",
            "user.transcript.final",
            "user.turn.committed",
            "assistant.status",
            "assistant.text.delta",
            "assistant.text.completed",
            "assistant.audio.delta",
            "assistant.audio.completed",
            "tool.started",
            "tool.completed",
            "interruption",
            "session.reconnecting",
            "session.reconnected",
            "error",
            "session.closed",
        }
        self.assertEqual(ALL_EVENT_TYPES, expected)


if __name__ == "__main__":
    unittest.main()
