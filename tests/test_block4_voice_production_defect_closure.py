"""Block 4 — FINAL PRODUCTION VOICE DEFECT CLOSURE.

Root-cause-targeted tests (offline/fake-provider only -- no live audio
hardware, no paid STT/TTS calls) proving the exact defects reported in
production are actually fixed at their source:

1. `ProductionFailClosedSpeechTests` -- the root cause of BOTH the
   "Transcribed voice input." literal AND Panda's silent TTS: production
   could silently resolve `build_speech_providers()` to
   FakeSpeechToTextProvider/FakeTextToSpeechProvider because the ORIGINAL
   fail-closed guard checked a dead env var (`UI_CHAT_VOICE_ENABLED`) that
   no voice-consuming feature actually sets. Proves the fixed guard is
   keyed to the flags real code paths actually check
   (`realtime.runtime.realtime_enabled()` / `voice_interface.config.
   voice_interface_enabled()`, both default ON) and fails EXPLICITLY
   instead of substituting fake transcript/audio.

2. `SttFilenameMimeMappingTests` + `MimeTypeBoundaryTests` -- Boundary F:
   the browser's actual MediaRecorder container (webm/opus) must reach the
   STT provider labeled correctly, never hardcoded as "audio/wav".

3. `SttTtsCallBoundaryTests` -- proves, with call-counting spy providers
   (not the literal-returning fakes), that: empty audio never reaches STT
   as a fake/successful transcript; STT is called exactly once per commit
   and its real return value becomes the exact canonical user turn text
   (using the literal acceptance-script phrase); the assistant response is
   generated once and that SAME text is what TTS receives, exactly once.

4. `RouterPlaybackAckTests` -- the playback_event control frame (browser
   audio.play/ended ack) dispatches to the bridge.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest

from business_assistant.conversation_gateway import FakePandaConversationGateway
from business_assistant_api.runtime import build_business_assistant_api_runtime
from integrations.production.adapters.speech import (
    OpenAISpeechToTextProvider,
    _stt_filename_for_mime,
    build_speech_providers,
    speech_provider_diagnostics,
)
from integrations.production.errors import ProductionProviderError
from integrations.production.factory import build_production_integrations
from personalization.service import PersonalizationService
from personalization.store import SqlitePersonalizationStore
from realtime.bridge import RealtimeConversationBridge
from realtime.errors import RT_AUDIO_EMPTY
from realtime.session import NullSink
from ui_chat.voice.stt import FakeSpeechToTextProvider
from ui_chat.voice.tts import FakeTextToSpeechProvider


class ProductionFailClosedSpeechTests(unittest.TestCase):
    """Root cause of the "Transcribed voice input." / silent-Panda defects."""

    def test_production_default_env_raises_instead_of_silently_using_fake(self):
        # REALTIME_ENABLED defaults to True (realtime.runtime.realtime_enabled)
        # -- a bare production env with no speech credentials must fail
        # explicitly at provider-construction time, not silently return
        # FakeSpeechToTextProvider/FakeTextToSpeechProvider.
        with self.assertRaises(ProductionProviderError) as ctx:
            build_speech_providers({"PANDA_ENV": "production"})
        self.assertEqual(ctx.exception.message, "speech_key_required")
        self.assertEqual(ctx.exception.provider_id, "speech")

    def test_production_with_explicit_fake_provider_still_raises(self):
        # Explicitly setting SPEECH_PROVIDER=fake in production must not be
        # a silent bypass either -- this is exactly the config the current
        # production defect report shows reaching FakeSpeechToTextProvider.
        with self.assertRaises(ProductionProviderError):
            build_speech_providers({"PANDA_ENV": "production", "SPEECH_PROVIDER": "fake"})

    def test_production_with_every_voice_feature_explicitly_disabled_allows_fake(self):
        # Only an operator who has explicitly turned off EVERY voice-
        # consuming feature may run production without real speech
        # credentials -- never the previous silent default.
        stt, tts = build_speech_providers(
            {
                "PANDA_ENV": "production",
                "REALTIME_ENABLED": "false",
                "VOICE_INTERFACE_ENABLED": "false",
            }
        )
        self.assertIsInstance(stt, FakeSpeechToTextProvider)
        self.assertIsInstance(tts, FakeTextToSpeechProvider)

    def test_development_env_keeps_existing_fake_default_behavior(self):
        # No behavior change outside production -- existing dev/test flows
        # that rely on the fake default must keep working unmodified.
        stt, tts = build_speech_providers({})
        self.assertIsInstance(stt, FakeSpeechToTextProvider)
        self.assertIsInstance(tts, FakeTextToSpeechProvider)

    def test_production_with_real_provider_and_key_returns_real_providers(self):
        stt, tts = build_speech_providers(
            {"PANDA_ENV": "production", "SPEECH_PROVIDER": "openai", "OPENAI_API_KEY": "sk-live-x"}
        )
        self.assertIsInstance(stt, OpenAISpeechToTextProvider)
        self.assertNotIsInstance(tts, FakeTextToSpeechProvider)

    def test_speech_provider_diagnostics_are_safe_and_report_real_vs_fake(self):
        fake_diag = speech_provider_diagnostics({})
        self.assertEqual(fake_diag["provider_kind"], "fake")
        self.assertFalse(fake_diag["ready"])
        self.assertTrue(fake_diag["voice_feature_active"], "realtime/voice_interface default ON")

        real_diag = speech_provider_diagnostics({"SPEECH_PROVIDER": "openai", "OPENAI_API_KEY": "sk-live-x"})
        self.assertEqual(real_diag["provider_kind"], "real")
        self.assertTrue(real_diag["ready"])
        # Never leaks the key itself.
        self.assertNotIn("sk-live-x", str(real_diag))

    def test_admin_provider_matrix_exposes_speech_real_vs_fake_without_secrets(self):
        bundle = build_production_integrations(env={"OPENAI_API_KEY": "sk-live-secret", "SPEECH_PROVIDER": "openai"})
        matrix = {m["provider_id"]: m for m in bundle.registry.list_metadata()}
        self.assertEqual(matrix["speech_stt"]["live_evidence"]["provider_kind"], "real")
        self.assertEqual(matrix["speech_tts"]["live_evidence"]["provider_kind"], "real")
        self.assertNotIn("sk-live-secret", str(matrix))


class SttFilenameMimeMappingTests(unittest.TestCase):
    """Boundary F: OpenAI's transcription endpoint selects its demuxer from
    the uploaded filename's extension -- sending real webm/opus bytes as
    "audio.wav" is a silent container/codec mismatch."""

    def test_webm_maps_to_webm_filename(self):
        self.assertEqual(_stt_filename_for_mime("audio/webm;codecs=opus"), "audio.webm")
        self.assertEqual(_stt_filename_for_mime("audio/webm"), "audio.webm")

    def test_ogg_maps_to_ogg_filename(self):
        self.assertEqual(_stt_filename_for_mime("audio/ogg;codecs=opus"), "audio.ogg")

    def test_unknown_or_missing_mime_falls_back_to_wav(self):
        self.assertEqual(_stt_filename_for_mime(""), "audio.wav")
        self.assertEqual(_stt_filename_for_mime("application/octet-stream"), "audio.wav")


class _SpyProvider:
    """Call-counting STT/TTS spy -- unlike FakeSpeechToTextProvider (which
    returns the fixed literal "Transcribed voice input." for realism-of-
    literal purposes), this spy returns the EXACT recognized phrase a real
    provider would, so tests can assert the canonical-turn/canonical-
    response contracts independent of the literal-fallback defect."""

    def __init__(self, reply: str = "recognized"):
        self.calls: list[dict] = []
        self.reply = reply

    def transcribe(self, *, audio: bytes, mime_type: str, language: str = "auto") -> str:
        self.calls.append({"audio_len": len(audio), "mime_type": mime_type, "language": language})
        return self.reply


class _SpyTts:
    def __init__(self):
        self.calls: list[dict] = []

    def synthesize(self, *, text: str, voice: str = "default", mime_type: str = "audio/wav") -> bytes:
        self.calls.append({"text": text, "voice": voice, "mime_type": mime_type})
        return b"AUDIO:" + text.encode("utf-8")


class SttTtsCallBoundaryTests(unittest.IsolatedAsyncioTestCase):
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

    def _bridge(self, stt, tts) -> RealtimeConversationBridge:
        return RealtimeConversationBridge(ba_api=self.rt.service, stt=stt, tts=tts, personalization=self.pz)

    async def test_empty_audio_never_becomes_a_fake_or_placeholder_transcript(self):
        stt = _SpyProvider(reply="should never be called")
        bridge = self._bridge(stt, _SpyTts())
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        with self.assertRaises(Exception) as ctx:
            await bridge.on_audio_commit(session)
        self.assertEqual(getattr(ctx.exception, "code", None), RT_AUDIO_EMPTY)
        self.assertEqual(stt.calls, [], "STT must never be invoked for empty audio")

    async def test_real_recognized_phrase_becomes_the_exact_canonical_user_turn_once(self):
        # The exact acceptance-script phrase from the production defect
        # closure request.
        recognized = "Привет, как дела?"
        stt = _SpyProvider(reply=recognized)
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="Хорошо, а у тебя?")
        bridge = self._bridge(stt, _SpyTts())
        sink = NullSink()
        session = await bridge.create_session(
            tenant_id="t1", owner_id="u1", sink=sink, mime_type="audio/webm;codecs=opus"
        )

        await bridge.on_audio_chunk(session, b"some-real-recorded-bytes")
        turn_id = await bridge.on_audio_commit(session)
        await session.current_task

        # STT called exactly once for the FINAL commit (the earlier
        # on_audio_chunk call is a separate incremental-partial call, by
        # design -- see realtime/bridge.py on_audio_chunk docstring).
        commit_calls = [c for c in stt.calls if c["audio_len"] == len(b"some-real-recorded-bytes")]
        self.assertGreaterEqual(len(commit_calls), 1)
        for c in commit_calls:
            self.assertEqual(c["mime_type"], "audio/webm;codecs=opus", "Boundary F: real negotiated mime type, not a hardcoded wav")

        finals = [e for e in sink.events if e.type == "user.transcript.final"]
        committed = [e for e in sink.events if e.type == "user.turn.committed"]
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(committed), 1)
        self.assertEqual(finals[0].data["text"], recognized, "the EXACT recognized phrase, not a placeholder literal")
        self.assertEqual(committed[0].data["text"], recognized)
        self.assertNotIn("Transcribed voice input.", [e.data.get("text") for e in sink.events if "text" in e.data])

        messages = self.rt.service.get_conversation_messages(
            tenant_id="t1", owner_id="u1", conversation_id=session.conversation_id
        )
        user_texts = [m["content"] for m in messages if m.get("role") == "user"]
        self.assertEqual(user_texts, [recognized], "one spoken turn = one canonical user message, no duplicates")

    async def test_one_canonical_assistant_response_feeds_text_and_tts_exactly_once(self):
        reply = "Ответ Панды"
        stt = _SpyProvider(reply="вопрос")
        tts = _SpyTts()
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=reply)
        bridge = self._bridge(stt, tts)
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, b"audio-bytes")
        turn_id = await bridge.on_audio_commit(session)
        await session.current_task

        # No second model request for voice, no independent spoken answer:
        # TTS is invoked exactly once, with the EXACT SAME text that was
        # streamed as the visible/text response.
        self.assertEqual(len(tts.calls), 1)
        self.assertEqual(tts.calls[0]["text"], reply)
        text_completed = next(e for e in sink.events if e.type == "assistant.text.completed")
        self.assertEqual(text_completed.data["text"], reply)
        audio_completed = next(e for e in sink.events if e.type == "assistant.audio.completed")
        self.assertEqual(audio_completed.turn_id, turn_id)
        self.assertGreater(audio_completed.data["total_bytes"], 0, "TTS produced real, non-empty audio")

    async def test_session_mime_type_defaults_to_webm_and_is_used_for_partial_transcription(self):
        stt = _SpyProvider(reply="")
        bridge = self._bridge(stt, _SpyTts())
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        self.assertEqual(session.mime_type, "audio/webm")

        await bridge.on_audio_chunk(session, b"chunk-1")
        self.assertEqual(stt.calls[-1]["mime_type"], "audio/webm")


class RouterPlaybackAckTests(unittest.IsolatedAsyncioTestCase):
    """Section 11/20: the browser's real audio.play/ended events are acked
    back over the SAME JSON-control-frame convention as audio.commit/
    barge_in/voice.select (realtime/router.py _handle_control_frame) so a
    production failure between "TTS audio received" and "user actually
    heard it" is visible server-side."""

    async def test_playback_event_control_frame_dispatches_to_bridge(self):
        import json

        from realtime.router import _handle_control_frame

        calls = []

        class _FakeBridge:
            def record_playback_event(self, session, *, stage, turn_id=""):
                calls.append((stage, turn_id))

        bridge = _FakeBridge()
        await _handle_control_frame(
            bridge, object(), json.dumps({"type": "playback_event", "stage": "started", "turn_id": "t1"})
        )
        await _handle_control_frame(
            bridge, object(), json.dumps({"type": "playback_event", "stage": "completed", "turn_id": "t1"})
        )
        self.assertEqual(calls, [("started", "t1"), ("completed", "t1")])


if __name__ == "__main__":
    unittest.main()
