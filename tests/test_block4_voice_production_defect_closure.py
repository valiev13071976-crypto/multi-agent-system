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

5. `DefectBContinuousDialogueLatencyTests` -- proves a multi-sentence
   reply's TTS audio for the FIRST sentence streams to the client before
   the LAST sentence has even been synthesized (real streaming start, not
   "wait for everything"); and proves the full per-turn latency timeline
   (speech_start .. listening_resumed) is recorded with every required
   stage once a turn completes and the client acks playback/listening-
   resumed.

6. `PR22RootFixNoPerChunkSttTests` -- PR #22 "PRODUCTION ACCEPTANCE FAILED"
   ROOT FIX: on_audio_chunk() (the hot path realtime/router.py's single
   sequential WebSocket receive loop awaits for EVERY binary frame, ahead
   of reading audio.commit/barge_in/session.close) must NEVER call the
   real, paid, buffer-based STT provider -- the earlier "throttled partial
   re-transcription" approach was still calling it repeatedly per
   utterance (the actual reported "many /audio/transcriptions calls per
   reply, /responses only appears later" production defect). Proves: 40
   binary chunks -> audio.commit calls STT EXACTLY ONCE; chunk ingestion
   stays fast even against a deliberately slow STT provider (structural
   non-blocking proof, no live paid API calls); and a second turn produces
   a second, independent STT call with no leftover audio bleeding across
   turns.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
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
from realtime.bridge import RealtimeConversationBridge, _split_for_tts
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

        # Root fix (PR #22): on_audio_chunk never calls STT -- the ONE and
        # ONLY STT call for this whole physical utterance happens at
        # audio.commit.
        self.assertEqual(len(stt.calls), 1)
        self.assertEqual(stt.calls[0]["audio_len"], len(b"some-real-recorded-bytes"))
        self.assertEqual(
            stt.calls[0]["mime_type"], "audio/webm;codecs=opus", "Boundary F: real negotiated mime type, not a hardcoded wav"
        )

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

    async def test_session_mime_type_defaults_to_webm_and_is_used_for_the_final_commit_transcription(self):
        stt = _SpyProvider(reply="")
        bridge = self._bridge(stt, _SpyTts())
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        self.assertEqual(session.mime_type, "audio/webm")

        # Root fix (PR #22): on_audio_chunk never calls STT, regardless of
        # how many chunks arrive.
        await bridge.on_audio_chunk(session, b"chunk-1")
        await bridge.on_audio_chunk(session, b"chunk-2")
        self.assertEqual(stt.calls, [])

        await bridge.on_audio_commit(session)
        self.assertEqual(len(stt.calls), 1)
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


class _TimedSpyTts:
    """Like _SpyTts, but also records the wall-clock time of each
    synthesize() call so a test can prove audio for an EARLIER sentence
    reached the client before a LATER sentence was even synthesized."""

    def __init__(self):
        self.calls: list[dict] = []
        self.call_times: list[float] = []

    def synthesize(self, *, text: str, voice: str = "default", mime_type: str = "audio/wav") -> bytes:
        self.call_times.append(time.monotonic())
        self.calls.append({"text": text, "voice": voice, "mime_type": mime_type})
        return b"AUDIO:" + text.encode("utf-8")


class DefectBContinuousDialogueLatencyTests(unittest.IsolatedAsyncioTestCase):
    """DEFECT B -- voice mode must be a real continuous dialogue, not
    request/wait/response. See module docstring section 5. (The original
    "throttle the per-chunk partial-STT calls" mitigation tested here was
    superseded by the PR #22 root fix -- on_audio_chunk no longer calls STT
    at all; see PR22RootFixNoPerChunkSttTests below.)"""

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

    def test_split_for_tts_keeps_short_replies_as_one_chunk(self):
        # No regression for the common case: a short reply with no sentence
        # boundary is still ONE chunk (identical to pre-DEFECT-B behavior).
        self.assertEqual(_split_for_tts("Ответ Панды"), ["Ответ Панды"])
        self.assertEqual(_split_for_tts(""), [])

    def test_split_for_tts_splits_a_genuinely_multi_sentence_reply(self):
        text = "Первое предложение подлиннее. Второе предложение тоже подлиннее. Третье."
        chunks = _split_for_tts(text)
        self.assertGreater(len(chunks), 1, "a real multi-sentence reply must stream in more than one TTS call")
        self.assertEqual(" ".join(chunks).replace("  ", " "), text)

    async def test_multi_sentence_audio_streams_before_the_last_sentence_is_synthesized(self):
        reply = "Первое предложение подлиннее. Второе предложение тоже подлиннее. Третье подлиннее тоже."
        stt = _SpyProvider(reply="вопрос")
        tts = _TimedSpyTts()
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=reply)
        bridge = self._bridge(stt, tts)
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, b"audio-bytes")
        turn_id = await bridge.on_audio_commit(session)
        await session.current_task

        self.assertGreater(len(tts.calls), 1, "a genuinely multi-sentence reply must call TTS more than once")
        audio_deltas = [e for e in sink.events if e.type == "assistant.audio.delta"]
        self.assertGreater(len(audio_deltas), 0)
        audio_completed = next(e for e in sink.events if e.type == "assistant.audio.completed")
        expected_total = sum(len(b"AUDIO:" + c["text"].encode("utf-8")) for c in tts.calls)
        self.assertEqual(audio_completed.data["total_bytes"], expected_total)

        # The critical DEFECT B point-5 proof: the FIRST audio chunk was
        # marked as sent strictly BEFORE the LAST sentence was even handed
        # to the TTS provider -- i.e. real incremental streaming, not
        # "wait for the whole reply's audio, then send it all at once".
        self.assertIn("first_audio_chunk", session.turn_latency_marks)
        self.assertLess(
            session.turn_latency_marks["first_audio_chunk"],
            tts.call_times[-1],
            "first audio must stream to the client before the FINAL sentence is synthesized",
        )

    async def test_full_turn_latency_timeline_records_every_defect_b_stage(self):
        reply = "Хорошо, а у тебя?"
        stt = _SpyProvider(reply="Привет, как дела?")
        tts = _SpyTts()
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=reply)
        bridge = self._bridge(stt, tts)
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, b"audio-bytes")
        turn_id = await bridge.on_audio_commit(session)
        await session.current_task
        bridge.record_playback_event(session, stage="started", turn_id=turn_id)

        with self.assertLogs("realtime.voice_diagnostics", level="INFO") as cm:
            bridge.record_playback_event(session, stage="listening_resumed", turn_id=turn_id)

        timeline_records = [
            r for r in cm.records if getattr(r, "voice_event", {}).get("event") == "voice_turn_latency_timeline"
        ]
        self.assertEqual(len(timeline_records), 1)
        timeline = timeline_records[0].voice_event

        required_stages = [
            "speech_start",
            "speech_end",
            "stt_final",
            "turn_committed",
            "assistant_processing_started",
            "first_text_delta",
            "tts_started",
            "first_audio_chunk",
            "assistant_completed",
            "browser_playback_started",
            "listening_resumed",
        ]
        for stage in required_stages:
            key = f"{stage}_ms"
            self.assertIn(key, timeline, f"DEFECT B latency acceptance requires the '{stage}' stage timestamp")
            self.assertIsInstance(timeline[key], (int, float))

        # Real time.monotonic() is non-decreasing -- these stages were
        # marked in this exact real-world order in the test above, so their
        # relative-ms values must be non-decreasing too. This is the actual
        # "where does real time go" evidence DEFECT B demands.
        values = [timeline[f"{s}_ms"] for s in required_stages]
        self.assertEqual(values, sorted(values), "latency timeline stages must be in non-decreasing chronological order")


class PR22RootFixNoPerChunkSttTests(unittest.IsolatedAsyncioTestCase):
    """PR #22 "PRODUCTION ACCEPTANCE FAILED" ROOT FIX.

    Production/inspection of main proved the real architectural cause of
    "one reply -> many /audio/transcriptions calls, /responses only shows
    up later": on_audio_chunk() -- the hot path realtime/router.py's single
    sequential WebSocket receive loop awaits for EVERY binary MediaRecorder
    frame, ahead of reading the NEXT frame -- was calling the real, paid,
    buffer-based STT provider (throttled, but still repeatedly) instead of
    doing only cheap local buffering. The canonical turn invariant is: N
    binary audio chunks -> 1 audio.commit -> 1 final STT provider request
    -> 1 user turn -> 1 assistant response -> TTS -> playback -> LISTENING.
    These tests use fake/counting providers only -- no live paid API calls.
    """

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

    async def test_40_binary_audio_chunks_then_commit_calls_stt_exactly_once(self):
        # The main required proof: a real MediaRecorder emits a binary
        # chunk roughly every 250ms, so a several-second utterance is
        # dozens of chunks. Regardless of chunk count, the STT provider
        # must be called EXACTLY ONCE, at audio.commit -- never per chunk.
        stt = _SpyProvider(reply="Привет, как дела?")
        bridge = self._bridge(stt, _SpyTts())
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        for _ in range(40):
            await bridge.on_audio_chunk(session, b"chunk-bytes")
        self.assertEqual(stt.calls, [], "on_audio_chunk must never call STT, no matter how many chunks arrive")

        turn_id = await bridge.on_audio_commit(session)
        self.assertEqual(len(stt.calls), 1, "exactly ONE final STT call for the whole physical utterance")
        await session.current_task

        committed = [e for e in sink.events if e.type == "user.turn.committed"]
        finals = [e for e in sink.events if e.type == "user.transcript.final"]
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(finals), 1)
        self.assertEqual(committed[0].turn_id, turn_id)

    async def test_on_audio_chunk_ingestion_stays_fast_even_against_a_slow_stt_provider(self):
        # Proves audio.commit/barge_in/session.close are never blocked by a
        # remote STT round trip triggered from on_audio_chunk: a
        # deliberately slow (but still fake/offline) provider is used so
        # that IF on_audio_chunk ever called it, 40 chunks would take
        # seconds; because on_audio_chunk does only cheap local buffering,
        # ingesting all 40 chunks stays near-instant regardless of STT
        # provider speed.
        class _SlowProvider:
            def __init__(self):
                self.calls = 0

            def transcribe(self, *, audio: bytes, mime_type: str, language: str = "auto") -> str:
                self.calls += 1
                time.sleep(0.05)
                return "привет"

        stt = _SlowProvider()
        bridge = self._bridge(stt, _SpyTts())
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        started = time.monotonic()
        for _ in range(40):
            await bridge.on_audio_chunk(session, b"x")
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed,
            0.2,
            "on_audio_chunk must stay cheap and non-blocking regardless of STT provider speed "
            "(40 chunks * 50ms/call would be >= 2s if it called STT per chunk)",
        )
        self.assertEqual(stt.calls, 0)

        await bridge.on_audio_commit(session)
        self.assertEqual(stt.calls, 1)

    async def test_second_turn_calls_stt_again_with_no_leftover_audio_between_turns(self):
        stt = _SpyProvider(reply="первый вопрос")
        bridge = self._bridge(stt, _SpyTts())
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, b"turn-1-bytes")
        await bridge.on_audio_commit(session)
        await session.current_task
        self.assertEqual(bytes(session.audio_buffer), b"", "no leftover audio bytes remain after commit")

        stt.reply = "второй вопрос"
        await bridge.on_audio_chunk(session, b"turn-2-bytes-longer")
        await bridge.on_audio_commit(session)
        await session.current_task
        self.assertEqual(bytes(session.audio_buffer), b"", "no leftover audio bytes remain after the second commit")

        self.assertEqual(len(stt.calls), 2, "exactly one STT call per physical utterance, across turns")
        self.assertEqual(stt.calls[0]["audio_len"], len(b"turn-1-bytes"))
        self.assertEqual(
            stt.calls[1]["audio_len"],
            len(b"turn-2-bytes-longer"),
            "turn 2's audio must be exactly turn 2's bytes, never turn 1's leftover buffer",
        )

        finals = [e for e in sink.events if e.type == "user.transcript.final"]
        self.assertEqual([e.data["text"] for e in finals], ["первый вопрос", "второй вопрос"])
        committed = [e for e in sink.events if e.type == "user.turn.committed"]
        self.assertEqual(len(committed), 2)
        self.assertEqual(len(session.committed_turn_ids), 2)

    async def test_final_stt_reaches_assistant_text_and_tts_and_playback_can_resume_listening(self):
        # End-to-end proof of: final STT -> user.turn.committed -> assistant
        # processing -> assistant.text -> TTS -> assistant.audio -> browser
        # playback -> LISTENING, all on one committed turn.
        reply = "Хорошо, а у тебя?"
        stt = _SpyProvider(reply="Привет, как дела?")
        tts = _SpyTts()
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=reply)
        bridge = self._bridge(stt, tts)
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, b"audio-bytes")
        turn_id = await bridge.on_audio_commit(session)
        await session.current_task

        self.assertEqual(len(stt.calls), 1)
        text_completed = next(e for e in sink.events if e.type == "assistant.text.completed")
        self.assertEqual(text_completed.data["text"], reply)
        self.assertEqual(len(tts.calls), 1)
        self.assertEqual(tts.calls[0]["text"], reply)
        audio_completed = next(e for e in sink.events if e.type == "assistant.audio.completed")
        self.assertEqual(audio_completed.turn_id, turn_id)
        self.assertGreater(audio_completed.data["total_bytes"], 0)

        # Client acks that it actually played the audio and is listening
        # again -- the bridge must accept this without error and log the
        # completed per-turn latency timeline.
        bridge.record_playback_event(session, stage="started", turn_id=turn_id)
        with self.assertLogs("realtime.voice_diagnostics", level="INFO") as cm:
            bridge.record_playback_event(session, stage="listening_resumed", turn_id=turn_id)
        timeline_records = [
            r for r in cm.records if getattr(r, "voice_event", {}).get("event") == "voice_turn_latency_timeline"
        ]
        self.assertEqual(len(timeline_records), 1)
        self.assertIn("listening_resumed_ms", timeline_records[0].voice_event)


if __name__ == "__main__":
    unittest.main()
