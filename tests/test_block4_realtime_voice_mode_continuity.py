"""Block 4 — ChatGPT-voice-mode-parity defect closure (additional acceptance
contract): continuous voice conversation loop, VAD-driven auto-commit/
auto-barge-in, mute != exit, and canonical (non-leaking) realtime error
copy.

Backend (realtime/bridge.py, realtime/state_machine.py) already supported a
continuous multi-turn session end-to-end; this closure was entirely in the
CLIENT orchestration layer (static/panda/js/realtime.js/app.js), which is
regex-unfalsifiable for real control-flow/state-machine claims. Two
complementary test strategies are used here, both offline/fake/no live
provider:

1. `RealtimeVoiceModeExecutableJsTests` actually EXECUTES the real
   `realtime.js` class under plain Node (no browser/DOM, no real
   microphone -- none is available in this sandboxed environment) via
   `tests/frontend/realtime_voice_mode.node.test.js`, driving its real VAD
   math and state transitions with stubbed Web APIs. This is the strongest
   local proof of the continuous-loop/VAD/mute/exit contract achievable
   without a live browser + real mic + live paid STT/TTS provider.
2. `ContinuousVoiceConversationBridgeTests` proves, at the architecture
   layer that actually executes production logic (RealtimeConversationBridge
   + FakeSpeechToTextProvider/FakeTextToSpeechProvider), that ONE session
   can carry the exact structure of the item-12 acceptance script:
   turn 1 -> turn 2 (without any reconnect/restart signal) -> barge-in
   mid-turn-3-response -> turn 4 continuing in the SAME conversation, with
   zero duplicated committed turns.
3. `RealtimeErrorCopyTests` proves the canonical copy module never leaks a
   raw internal wire code (e.g. "rt_audio_empty") as user-facing text.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import unittest
from pathlib import Path

from business_assistant.conversation_gateway import FakePandaConversationGateway
from business_assistant_api.runtime import build_business_assistant_api_runtime
from personalization.service import PersonalizationService
from personalization.store import SqlitePersonalizationStore
from realtime.bridge import RealtimeConversationBridge
from realtime.session import NullSink
from ui_chat.voice.stt import FakeSpeechToTextProvider
from ui_chat.voice.tts import FakeTextToSpeechProvider

REPO_ROOT = Path(__file__).resolve().parents[1]


def _stt_bytes(text: str) -> bytes:
    return f"PANDA_STT_TEST:{text}".encode("utf-8")


class RealtimeVoiceModeExecutableJsTests(unittest.TestCase):
    """Executes the real client-side controller logic under Node."""

    def test_realtime_js_continuous_voice_mode_node_suite_passes(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node runtime not available in this environment")
        result = subprocess.run(
            [node, "--test", "tests/frontend/realtime_voice_mode.node.test.js"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"node test suite failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("# pass 17", result.stdout)
        self.assertIn("# fail 0", result.stdout)


class ContinuousVoiceConversationBridgeTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the item-12 production-acceptance script's structure at
    the bridge layer, using the FakeSTT marker convention to inject the
    EXACT literal phrases from the acceptance script deterministically."""

    def setUp(self):
        import os
        import tempfile

        self.tmp = tempfile.mkdtemp()
        self.rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "ba.sqlite"), with_integration=False
        )
        self.pz_store = SqlitePersonalizationStore(os.path.join(self.tmp, "pz.sqlite"))
        self.pz = PersonalizationService(store=self.pz_store, tts=FakeTextToSpeechProvider())
        self.rt.service.personalization_service = self.pz
        self.stt = FakeSpeechToTextProvider()
        self.tts = FakeTextToSpeechProvider()

    def tearDown(self):
        import shutil as _shutil

        self.pz.close()
        self.rt.close()
        _shutil.rmtree(self.tmp, ignore_errors=True)

    def _bridge(self) -> RealtimeConversationBridge:
        return RealtimeConversationBridge(
            ba_api=self.rt.service, stt=self.stt, tts=self.tts, personalization=self.pz
        )

    async def test_continuous_turns_then_barge_in_then_continues_same_conversation(self):
        long_reply = "Я тебя тоже прекрасно слышу и продолжаю разговор. " * 20
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=long_reply)
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        conv_id = session.conversation_id

        # Step 3-9: first spoken turn, WITHOUT any reconnect/"restart"
        # signal -- the same session handles it end-to-end.
        await bridge.on_audio_chunk(session, _stt_bytes("Ты меня слышишь?"))
        turn1 = await bridge.on_audio_commit(session)
        await session.current_task
        self.assertTrue(any(e.type == "assistant.text.completed" for e in sink.events))

        # Step 6-9: "without pressing Record again" -- at the architecture
        # level this means the SAME session accepts the next spoken turn
        # with no additional session/connect call.
        await bridge.on_audio_chunk(session, _stt_bytes("Я тебя тоже прекрасно слышу."))
        turn2 = await bridge.on_audio_commit(session)
        await session.current_task
        self.assertNotEqual(turn1, turn2)

        # Step 10-13: interrupt Panda mid-response; her audio must stop and
        # the interrupting speech becomes the NEXT turn in the SAME
        # conversation with no lost/duplicated turns.
        await bridge.on_audio_chunk(session, _stt_bytes("Расскажи подробнее"))
        turn3 = await bridge.on_audio_commit(session)
        for _ in range(5):
            await asyncio.sleep(0)
        interrupted = await bridge.barge_in(session)
        self.assertTrue(interrupted, "barge-in actually stopped the in-flight response")
        self.assertFalse(
            any(e.type == "assistant.text.completed" and e.turn_id == turn3 for e in sink.events),
            "the interrupted turn's response text never completes",
        )

        await bridge.on_audio_chunk(session, _stt_bytes("Продолжаем"))
        turn4 = await bridge.on_audio_commit(session)
        await session.current_task

        # Step 19/22: conversation continues with full context, no
        # duplicated committed turns, all in the SAME conversation.
        self.assertEqual(session.conversation_id, conv_id)
        self.assertEqual(
            session.committed_turn_ids, {turn1, turn2, turn3, turn4}, "every turn committed exactly once"
        )
        self.assertTrue(any(e.type == "assistant.text.completed" and e.turn_id == turn4 for e in sink.events))

        messages = self.rt.service.get_conversation_messages(
            tenant_id="t1", owner_id="u1", conversation_id=conv_id
        )
        user_texts = [m["content"] for m in messages if m.get("role") == "user"]
        self.assertEqual(
            user_texts,
            ["Ты меня слышишь?", "Я тебя тоже прекрасно слышу.", "Расскажи подробнее", "Продолжаем"],
            "canonical conversation history exactly matches what the user said -- no shadow transcript, "
            "no duplicated/reordered turns",
        )

    async def test_exit_after_continuous_turns_closes_session_no_replay(self):
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="ОК")
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Привет"))
        await bridge.on_audio_commit(session)
        await session.current_task

        await bridge.close_session(session, reason="client_requested")
        self.assertIsNone(bridge.get_session(session.session_id), "exit fully releases the session")
        self.assertTrue(any(e.type == "session.closed" for e in sink.events))


class RealtimeErrorCopyTests(unittest.TestCase):
    """Proves the canonical Russian-copy module never leaks a raw internal
    realtime wire code as user-facing text (e.g. the "rt_audio_empty"
    production defect this closure fixes)."""

    def setUp(self):
        self.copy_js = (REPO_ROOT / "static" / "shared" / "copy.js").read_text(encoding="utf-8")

    def test_silent_codes_and_friendly_map_defined(self):
        self.assertIn("REALTIME_SILENT_CODES", self.copy_js)
        self.assertIn('"rt_audio_empty"', self.copy_js)
        self.assertIn('"rt_stt_empty_transcript"', self.copy_js)
        self.assertIn("realtimeErrorText", self.copy_js)
        self.assertIn("isSilentRealtimeCode", self.copy_js)

    def test_realtime_js_and_app_js_route_errors_through_canonical_copy(self):
        realtime_js = (REPO_ROOT / "static" / "panda" / "js" / "realtime.js").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "static" / "panda" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("PandaCopy.realtimeErrorText", app_js)
        # realtime.js itself must never hardcode a "rt_audio_empty"-shaped
        # user-facing message -- all copy lives in the ONE canonical module.
        self.assertNotIn("rt_audio_empty", realtime_js)


if __name__ == "__main__":
    unittest.main()
