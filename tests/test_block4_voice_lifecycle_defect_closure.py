"""PANDA VOICE — PR #24: production lifecycle defect closure.

Safe Barge-In + Turn Ownership + Non-Blocking Speech I/O + Production
Diagnostics. Targeted tests only (fake/counting providers, no live paid
OpenAI calls), implementing the section-12 test plan A-I (test J -- frontend
VAD ownership -- lives in tests/frontend/realtime_voice_mode.node.test.js,
run via tests/test_block4_realtime_voice_mode_continuity.py).

Section 2/inspection summary (see realtime/bridge.py's module docstring for
the full writeup):

* A. Preserves PR #23 (40 chunks -> 1 commit -> exactly 1 STT call, no STT
  from on_audio_chunk).
* B. CONFIRMED DEFECT FIX: ordinary audio while the assistant is presenting
  no longer triggers an implicit barge-in.
* C. Explicit barge-in still cancels exactly the active presentation and
  emits exactly one interruption acknowledgement.
* D. Ownership: at most one presentation-owning task per session, proven
  even against a direct commit_turn() misuse (defense-in-depth guard).
* E. An interrupted turn never emits authoritative output after ownership
  moves to the next turn (shielded business work may still finish in the
  background, but its result never reaches the sink for a superseded turn).
* F. Stale client completion is a FRONTEND concern -- see the Node test
  suite referenced above.
* G. Barge-in exactly between assistant.text.completed and the first TTS
  call terminates cleanly as an intentional interruption.
* H. CONFIRMED DEFECT FIX: STT/TTS provider calls no longer block the
  event loop -- a slow provider in one session cannot stall an unrelated
  session's turn.
* I. Two sequential turns: no overlap, no stale events, one STT each.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest

from business_assistant.conversation_gateway import FakePandaConversationGateway
from business_assistant_api.runtime import build_business_assistant_api_runtime
from personalization.service import PersonalizationService
from personalization.store import SqlitePersonalizationStore
from realtime.bridge import RealtimeConversationBridge
from realtime.session import NullSink
from realtime.state_machine import SessionState
from tests.test_block4_voice_production_defect_closure import _SpyProvider, _SpyTts
from ui_chat.voice.tts import FakeTextToSpeechProvider


def _stt_bytes(text: str) -> bytes:
    return f"PANDA_STT_TEST:{text}".encode("utf-8")


class LifecycleDefectClosureTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "ba.sqlite"), with_integration=False
        )
        self.pz_store = SqlitePersonalizationStore(os.path.join(self.tmp, "pz.sqlite"))
        self.pz = PersonalizationService(store=self.pz_store, tts=FakeTextToSpeechProvider())
        self.rt.service.personalization_service = self.pz

    def tearDown(self):
        self.pz.close()
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bridge(self, stt=None, tts=None) -> RealtimeConversationBridge:
        return RealtimeConversationBridge(
            ba_api=self.rt.service,
            stt=stt or _SpyProvider(reply="привет"),
            tts=tts or _SpyTts(),
            personalization=self.pz,
        )


class TestAPreservePr23OneFinalSttPerUtterance(LifecycleDefectClosureTestBase):
    async def test_40_chunks_then_commit_calls_stt_exactly_once_no_partials(self):
        stt = _SpyProvider(reply="Привет, как дела?")
        bridge = self._bridge(stt=stt)
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        for _ in range(40):
            await bridge.on_audio_chunk(session, b"chunk-bytes")
        self.assertEqual(stt.calls, [], "on_audio_chunk must never call STT")

        await bridge.on_audio_commit(session)
        self.assertEqual(len(stt.calls), 1, "exactly ONE final STT call per physical utterance")
        await session.current_task
        partials = [e for e in sink.events if e.type == "user.transcript.partial"]
        self.assertEqual(partials, [], "PR #23 architecture preserved: no partial transcripts")


class TestBOrdinaryAudioIsNotInterruptionAuthority(LifecycleDefectClosureTestBase):
    async def test_ordinary_audio_while_assistant_active_does_not_cancel_the_turn(self):
        long_reply = "Ответ. " * 200
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=long_reply)
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, _stt_bytes("Расскажи подробно"))
        turn_id = await bridge.on_audio_commit(session)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertIn(
            session.state_machine.state,
            {SessionState.ASSISTANT_STREAMING_TEXT, SessionState.ASSISTANT_SPEAKING},
            "the assistant turn is genuinely active/presenting",
        )

        # Ordinary binary audio arrives -- background noise, echo, a
        # MediaRecorder artifact -- with NO explicit barge_in control frame.
        # This must NOT be treated as interruption authority (CONFIRMED
        # DEFECT FIX): the active task must still be running afterward.
        for _ in range(10):
            await bridge.on_audio_chunk(session, b"ordinary-noise-bytes")

        self.assertFalse(session.current_task.done(), "ordinary audio must never cancel the active presentation")
        self.assertEqual(bytes(session.audio_buffer), b"", "ordinary audio during presentation must be dropped, not buffered")
        self.assertEqual(sum(1 for e in sink.events if e.type == "interruption"), 0, "no unauthorized interruption fired")

        # The turn is allowed to run to completion undisturbed.
        interrupted = await bridge.barge_in(session)
        self.assertTrue(interrupted, "an EXPLICIT barge-in still works normally afterward")
        self.assertFalse(any(e.type == "assistant.text.completed" and e.turn_id == turn_id for e in sink.events))


class TestCExplicitBargeIn(LifecycleDefectClosureTestBase):
    async def test_explicit_barge_in_cancels_exactly_the_active_presentation_once(self):
        long_reply = "Ответ. " * 200
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=long_reply)
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, _stt_bytes("Расскажи подробно"))
        turn_id = await bridge.on_audio_commit(session)
        for _ in range(5):
            await asyncio.sleep(0)

        interrupted = await bridge.barge_in(session)
        self.assertTrue(interrupted)
        interruption_events = [e for e in sink.events if e.type == "interruption"]
        self.assertEqual(len(interruption_events), 1, "exactly one interruption acknowledgement")
        self.assertEqual(interruption_events[0].turn_id, turn_id)
        self.assertTrue(session.current_task.done())

        # A second barge-in with nothing active is a safe no-op.
        interrupted_again = await bridge.barge_in(session)
        self.assertFalse(interrupted_again)
        self.assertEqual(sum(1 for e in sink.events if e.type == "interruption"), 1)


class TestDPresentationOwnershipInvariant(LifecycleDefectClosureTestBase):
    async def test_at_most_one_presentation_owning_task_per_session(self):
        long_reply = "Ответ. " * 50
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=long_reply)
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        turn_a = await bridge.commit_turn(session, text="Первый вопрос", is_voice=False)
        task_a = session.current_task
        await asyncio.sleep(0)  # let turn A start emitting events

        # Directly calling commit_turn() again WITHOUT an intervening
        # barge_in exercises the defense-in-depth ownership guard itself --
        # every real client path (on_audio_commit/on_text_message) already
        # prevents this from happening; this proves the invariant holds
        # even if that gating were ever bypassed by a future change.
        turn_b = await bridge.commit_turn(session, text="Второй вопрос", is_voice=False)

        self.assertNotEqual(turn_a, turn_b)
        self.assertTrue(task_a.done(), "the stale task must be fully finished, never left running")
        task_b = session.current_task
        self.assertIsNot(task_a, task_b)
        self.assertEqual(session.current_turn_id, turn_b)
        await task_b

        self.assertTrue(any(e.type == "assistant.text.completed" and e.turn_id == turn_b for e in sink.events))
        # Turn A must never have reached text.completed once superseded.
        self.assertFalse(any(e.type == "assistant.text.completed" and e.turn_id == turn_a for e in sink.events))


class TestEInterruptedTurnNeverEmitsStaleOutput(LifecycleDefectClosureTestBase):
    async def test_interrupted_turn_never_emits_authoritative_output_after_ownership_moves_on(self):
        long_reply = "Ответ. " * 200
        gateway = FakePandaConversationGateway(response=long_reply)
        self.rt.service.ba.conversation_gateway = gateway
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, _stt_bytes("Расскажи подробно"))
        turn_a = await bridge.on_audio_commit(session)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertTrue(
            any(e.turn_id == turn_a and e.type == "assistant.text.delta" for e in sink.events),
            "turn A must have genuinely started streaming before being interrupted",
        )

        interrupted = await bridge.barge_in(session)
        self.assertTrue(interrupted)

        # Give the (shielded, still-finishing-in-the-background) business
        # call and any leftover scheduling every opportunity to leak a
        # stale event before we move on to turn B.
        for _ in range(20):
            await asyncio.sleep(0)

        gateway.response = "Второй ответ"
        await bridge.on_audio_chunk(session, _stt_bytes("Другой вопрос"))
        turn_b = await bridge.on_audio_commit(session)
        await session.current_task

        turn_a_events_after = [e for e in sink.events if e.turn_id == turn_a]
        self.assertFalse(
            any(e.type == "assistant.text.completed" for e in turn_a_events_after),
            "an interrupted turn must never reach text.completed after ownership moved on",
        )
        self.assertFalse(
            any(e.type in ("assistant.audio.delta", "assistant.audio.completed") for e in turn_a_events_after),
            "an interrupted turn must never emit audio after ownership moved on",
        )

        turn_b_completed = [e for e in sink.events if e.turn_id == turn_b and e.type == "assistant.text.completed"]
        self.assertEqual(len(turn_b_completed), 1)
        self.assertEqual(turn_b_completed[0].data["text"], "Второй ответ")
        self.assertEqual(len(gateway.calls), 2, "turn A's business call happened once, turn B's happened once")


class TestGBargeInBetweenTextCompletedAndTtsStart(LifecycleDefectClosureTestBase):
    async def test_barge_in_exactly_between_text_completed_and_first_tts_start_is_a_clean_interruption(self):
        reply = "Первое предложение подлиннее текста. Второе предложение тоже подлиннее текста."
        stt = _SpyProvider(reply="вопрос")
        tts_started = threading.Event()
        proceed = threading.Event()

        class _PausingTts:
            def synthesize(self, *, text: str, voice: str = "default", mime_type: str = "audio/mpeg") -> bytes:
                tts_started.set()
                proceed.wait(timeout=2.0)
                return b"AUDIO:" + text.encode("utf-8")

        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=reply)
        bridge = self._bridge(stt=stt, tts=_PausingTts())
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, b"audio-bytes")
        turn_id = await bridge.on_audio_commit(session)

        try:
            # Poll (never blocking the event loop) until the TTS call has
            # actually started running in its worker thread -- proves the
            # interruption below lands exactly in the text->TTS boundary
            # window described in section 9, only reachable at all because
            # of the section-7 non-blocking-I/O fix (asyncio.to_thread).
            deadline = time.monotonic() + 2.0
            while not tts_started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            self.assertTrue(tts_started.is_set(), "TTS call never started")

            text_completed = [e for e in sink.events if e.type == "assistant.text.completed"]
            self.assertEqual(len(text_completed), 1, "text was fully emitted before TTS started")

            interrupted = await bridge.barge_in(session)
            self.assertTrue(interrupted)
        finally:
            proceed.set()  # let the paused worker thread finish so it doesn't leak

        self.assertEqual(sum(1 for e in sink.events if e.type == "interruption"), 1)
        self.assertEqual(
            [e for e in sink.events if e.turn_id == turn_id and e.type == "assistant.audio.completed"],
            [],
            "no audio.completed for a turn interrupted before any audio was produced",
        )


class TestHNonBlockingSttTts(LifecycleDefectClosureTestBase):
    async def test_slow_stt_provider_does_not_block_unrelated_sessions_event_loop_progress(self):
        class _SlowStt:
            def transcribe(self, *, audio: bytes, mime_type: str, language: str = "auto") -> str:
                time.sleep(0.3)
                return "session A speech"

        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="ok")
        bridge = self._bridge(stt=_SlowStt(), tts=_SpyTts())
        sink_a = NullSink()
        sink_b = NullSink()
        session_a = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink_a)
        session_b = await bridge.create_session(tenant_id="t1", owner_id="u2", sink=sink_b)

        await bridge.on_audio_chunk(session_a, b"audio-a")

        t0 = time.monotonic()
        turn_a_task = asyncio.create_task(bridge.on_audio_commit(session_a))
        # Give the slow STT call a moment to actually start running in its
        # background thread before starting the unrelated session B work.
        await asyncio.sleep(0.03)
        turn_b_id = await bridge.on_text_message(session_b, text="Привет от другой сессии")
        elapsed_b = time.monotonic() - t0

        self.assertLess(
            elapsed_b,
            0.25,
            "session B's unrelated turn must complete without waiting for session A's slow STT call "
            f"(took {elapsed_b:.3f}s -- would be >= 0.3s if the event loop were blocked)",
        )
        await session_b.current_task
        self.assertTrue(any(e.type == "assistant.text.completed" for e in sink_b.events))

        await turn_a_task
        await session_a.current_task
        self.assertTrue(any(e.type == "user.transcript.final" and e.data["text"] == "session A speech" for e in sink_a.events))

    async def test_slow_tts_provider_does_not_block_unrelated_sessions_event_loop_progress(self):
        class _SlowTts:
            def synthesize(self, *, text: str, voice: str = "default", mime_type: str = "audio/mpeg") -> bytes:
                time.sleep(0.3)
                return b"AUDIO:" + text.encode("utf-8")

        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="Ответ А")
        bridge = self._bridge(stt=_SpyProvider(reply="вопрос А"), tts=_SlowTts())
        sink_a = NullSink()
        sink_b = NullSink()
        session_a = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink_a)
        session_b = await bridge.create_session(tenant_id="t1", owner_id="u2", sink=sink_b)

        await bridge.on_audio_chunk(session_a, b"audio-a")

        t0 = time.monotonic()
        commit_a_task = asyncio.create_task(bridge.on_audio_commit(session_a))
        await asyncio.sleep(0.03)
        # Session A's slow TTS call is now running in a background thread
        # (inside session_a.current_task, created by on_audio_commit's
        # commit_turn -> _run_turn chain) -- session B's unrelated text turn
        # must still proceed promptly.
        turn_b_id = await bridge.on_text_message(session_b, text="Привет от другой сессии")
        elapsed_b = time.monotonic() - t0
        self.assertLess(elapsed_b, 0.25, f"session B must not wait on session A's slow TTS call (took {elapsed_b:.3f}s)")

        await session_b.current_task
        await commit_a_task
        await session_a.current_task
        self.assertTrue(any(e.type == "assistant.audio.completed" for e in sink_a.events))


class TestITwoSequentialTurnsNoOverlap(LifecycleDefectClosureTestBase):
    async def test_two_sequential_turns_no_overlap_no_stale_events_one_stt_each(self):
        gateway = FakePandaConversationGateway(response="Ответ А")
        self.rt.service.ba.conversation_gateway = gateway
        stt = _SpyProvider(reply="Вопрос А")
        bridge = self._bridge(stt=stt, tts=_SpyTts())
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, b"audio-a")
        turn_a = await bridge.on_audio_commit(session)
        await session.current_task
        self.assertTrue(session.current_task.done())
        self.assertEqual(session.state_machine.state, SessionState.LISTENING)

        gateway.response = "Ответ Б"
        stt.reply = "Вопрос Б"
        await bridge.on_audio_chunk(session, b"audio-b")
        turn_b = await bridge.on_audio_commit(session)
        await session.current_task

        self.assertNotEqual(turn_a, turn_b)
        self.assertEqual(len(stt.calls), 2, "one STT call per physical utterance, across turns")
        turn_a_completed = [e for e in sink.events if e.turn_id == turn_a and e.type == "assistant.text.completed"]
        turn_b_completed = [e for e in sink.events if e.turn_id == turn_b and e.type == "assistant.text.completed"]
        self.assertEqual(len(turn_a_completed), 1)
        self.assertEqual(len(turn_b_completed), 1)
        self.assertEqual(turn_a_completed[0].data["text"], "Ответ А")
        self.assertEqual(turn_b_completed[0].data["text"], "Ответ Б")
        self.assertEqual(session.state_machine.state, SessionState.LISTENING)
        self.assertEqual(session.committed_turn_ids, {turn_a, turn_b})


class TestObservabilityStructuredPayloadReachesMessageText(unittest.IsolatedAsyncioTestCase):
    """CONFIRMED OBSERVABILITY DEFECT: main.py's root logging config format
    string never references %(voice_event)s, so the structured payload was
    silently dropped from every actually-rendered log line -- only the
    literal string "realtime_voice_event" ever reached stdout. Proves the
    fix: the SAME safe/bounded fields now appear in the rendered message
    text itself (what a real formatter/handler actually emits), not just
    as an in-memory LogRecord attribute only visible to test introspection."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "ba.sqlite"), with_integration=False
        )
        self.pz_store = SqlitePersonalizationStore(os.path.join(self.tmp, "pz.sqlite"))
        self.pz = PersonalizationService(store=self.pz_store, tts=FakeTextToSpeechProvider())
        self.rt.service.personalization_service = self.pz

    def tearDown(self):
        self.pz.close()
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_turn_id_and_lifecycle_stage_survive_into_the_rendered_log_message(self):
        bridge = RealtimeConversationBridge(
            ba_api=self.rt.service, stt=_SpyProvider(reply="Привет"), tts=_SpyTts(), personalization=self.pz
        )
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        with self.assertLogs("realtime.voice_diagnostics", level="INFO") as cm:
            await bridge.on_audio_chunk(session, b"audio-bytes")
            turn_id = await bridge.on_audio_commit(session)
            await session.current_task

        committed_record = next(
            r for r in cm.records if getattr(r, "voice_event", {}).get("event") == "voice_turn_committed"
        )
        rendered = committed_record.getMessage()
        self.assertIn(turn_id, rendered, "turn_id must be present in the ACTUAL rendered log message text")
        self.assertIn(session.conversation_id, rendered)
        self.assertIn(session.session_id, rendered)
        self.assertIn("voice_turn_committed", rendered)
        # No transcript/message content ever appears in the diagnostic line.
        self.assertNotIn("Привет", rendered)


if __name__ == "__main__":
    unittest.main()
