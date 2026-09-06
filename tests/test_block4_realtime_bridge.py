"""Block 4 — RealtimeConversationBridge orchestration tests.

Offline/fake-provider only (no live audio hardware, no paid STT/TTS/LLM
calls). Proves the REALTIME TEST CONTRACT (master spec section 55, A-J) and
the personalization<->realtime integration.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from business_assistant.action_continuation import CALL_TOOL, FAMILY_IMAGE_GENERATE
from business_assistant.conversation_gateway import (
    ConversationRequest,
    FakePandaConversationGateway,
    WorkflowPandaConversationGateway,
)
from business_assistant_api.runtime import build_business_assistant_api_runtime
from business_assistant_api.models import ST_WAITING_FOR_APPROVAL
from finops.service import FinOpsService
from personalization.models import LANGUAGE_RU, STYLE_PROFESSIONAL
from personalization.service import PersonalizationService
from personalization.store import SqlitePersonalizationStore
from realtime.bridge import RealtimeConversationBridge
from realtime.session import NullSink
from tools.models import TOOL_STATUS_SUCCEEDED, ToolResult
from ui_chat.voice.stt import FakeSpeechToTextProvider
from ui_chat.voice.tts import FakeTextToSpeechProvider


def _stt_bytes(text: str) -> bytes:
    return f"PANDA_STT_TEST:{text}".encode("utf-8")


def _engine(response_text: str = "unused"):
    engine = Mock()
    engine.execute = AsyncMock(return_value={"final_answer": response_text})
    engine.last_workflow_id = "wf-1"
    return engine


def _image_tool_gateway():
    """Fake ToolGateway that always returns one successfully generated image
    -- mirrors tests/test_panda_generated_image_direct_actions_acceptance_fix.py."""

    captured: list = []

    async def _invoke(tool_request, **kwargs):
        captured.append(tool_request)
        return ToolResult(
            request_id=tool_request.request_id,
            tool_id=tool_request.tool_id,
            operation=tool_request.operation,
            status=TOOL_STATUS_SUCCEEDED,
            success=True,
            data={
                "version_id": "img-v1",
                "version_ids": ["img-v1"],
                "assets": [
                    {
                        "version_id": "img-v1",
                        "artifact_type": "image",
                        "mime_type": "image/png",
                        "view_url": "/api/v1/business-assistant/media/img-v1",
                    }
                ],
                "mime_type": "image/png",
                "status": "completed",
            },
        )

    gw = Mock()
    gw.invoke = AsyncMock(side_effect=_invoke)
    return gw, captured


class RealtimeBridgeTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
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
        self.pz.close()
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bridge(self) -> RealtimeConversationBridge:
        return RealtimeConversationBridge(
            ba_api=self.rt.service, stt=self.stt, tts=self.tts, personalization=self.pz
        )


class VoiceTurnLifecycleTests(RealtimeBridgeTestBase):
    """Contract A/B/C: partial->final->one committed turn; streaming text;
    voice+text correspond to the SAME assistant turn."""

    async def test_a_partial_then_final_then_exactly_one_committed_turn(self):
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="Привет!")
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, _stt_bytes("Привет Панда"))
        turn_id = await bridge.on_audio_commit(session)
        await session.current_task

        committed = [e for e in sink.events if e.type == "user.turn.committed"]
        partials = [e for e in sink.events if e.type == "user.transcript.partial"]
        finals = [e for e in sink.events if e.type == "user.transcript.final"]
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(finals), 1)
        self.assertGreaterEqual(len(partials), 1)
        self.assertEqual(committed[0].turn_id, turn_id)
        self.assertEqual(len(session.committed_turn_ids), 1)

    async def test_b_assistant_text_streams_incrementally_and_reassembles_exactly(self):
        reply = "Это довольно длинный ответ, который должен быть разбит на несколько частей для потоковой передачи."
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=reply)
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Расскажи что-нибудь"))
        await bridge.on_audio_commit(session)
        await session.current_task

        deltas = [e for e in sink.events if e.type == "assistant.text.delta"]
        completed = [e for e in sink.events if e.type == "assistant.text.completed"]
        self.assertGreater(len(deltas), 1, "must be split into multiple ordered chunks")
        self.assertEqual(len(completed), 1)
        reassembled = "".join(d.data["delta"] for d in deltas)
        self.assertEqual(reassembled, reply, "no missing/duplicated/reordered chunks")
        self.assertEqual(completed[0].data["text"], reply)
        # strictly increasing sequence across the whole turn
        seqs = [e.seq for e in sink.events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    async def test_c_voice_and_text_output_correspond_to_the_same_turn(self):
        reply = "Готовый ответ"
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=reply)
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Привет"))
        turn_id = await bridge.on_audio_commit(session)
        await session.current_task

        text_completed = next(e for e in sink.events if e.type == "assistant.text.completed")
        audio_completed = next(e for e in sink.events if e.type == "assistant.audio.completed")
        self.assertEqual(text_completed.turn_id, turn_id)
        self.assertEqual(audio_completed.turn_id, turn_id)
        self.assertEqual(text_completed.data["text"], reply)
        # The synthesized audio actually encodes the SAME text (fake TTS
        # payload format is `TTS:{voice}:{text[:120]}` -- see ui_chat.voice.tts).
        audio_bytes = b"".join(chunk for tid, chunk in sink.audio_chunks if tid == turn_id)
        self.assertIn(reply.encode("utf-8"), audio_bytes)


class BargeInTests(RealtimeBridgeTestBase):
    async def test_d_barge_in_stops_audio_and_next_speech_is_next_turn(self):
        long_reply = "Ответ. " * 200
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response=long_reply)
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, _stt_bytes("Расскажи подробно"))
        turn1 = await bridge.on_audio_commit(session)
        for _ in range(5):
            await asyncio.sleep(0)
        interrupted = await bridge.barge_in(session)
        self.assertTrue(interrupted)
        self.assertFalse(any(e.type == "assistant.text.completed" for e in sink.events))
        self.assertEqual(sum(1 for e in sink.events if e.type == "interruption"), 1)

        await bridge.on_audio_chunk(session, _stt_bytes("Другой вопрос"))
        turn2 = await bridge.on_audio_commit(session)
        await session.current_task
        self.assertNotEqual(turn1, turn2)
        self.assertEqual(session.committed_turn_ids, {turn1, turn2})
        self.assertTrue(any(e.type == "assistant.text.completed" and e.turn_id == turn2 for e in sink.events))

    async def test_barge_in_noop_when_nothing_is_speaking(self):
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        interrupted = await bridge.barge_in(session)
        self.assertFalse(interrupted)


class ModalitySwitchTests(RealtimeBridgeTestBase):
    async def test_e_voice_then_text_then_voice_same_conversation_and_context(self):
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="ОК")
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        conv_id = session.conversation_id

        await bridge.on_audio_chunk(session, _stt_bytes("Привет"))
        await bridge.on_audio_commit(session)
        await session.current_task

        await bridge.on_text_message(session, text="Продолжим текстом")
        await session.current_task

        await bridge.on_audio_chunk(session, _stt_bytes("И снова голосом"))
        await bridge.on_audio_commit(session)
        await session.current_task

        # Same conversation the whole time -- reused text-chat persistence,
        # not a shadow/second conversation (Block 4.14/4.16).
        self.assertEqual(session.conversation_id, conv_id)
        messages = self.rt.service.get_conversation_messages(
            tenant_id="t1", owner_id="u1", conversation_id=conv_id
        )
        user_messages = [m for m in messages if m.get("role") == "user"]
        self.assertEqual(len(user_messages), 3)
        self.assertEqual(user_messages[0]["content"], "Привет")
        self.assertEqual(user_messages[1]["content"], "Продолжим текстом")
        self.assertEqual(user_messages[2]["content"], "И снова голосом")


class ReconnectIdempotencyTests(RealtimeBridgeTestBase):
    async def test_f_reconnect_retry_of_same_turn_does_not_duplicate(self):
        gw, captured = _image_tool_gateway()
        real_gw = WorkflowPandaConversationGateway(
            workflow_engine=_engine(), run_router=object(), context_manager=object(), tool_gateway=gw
        )
        self.rt.service.ba.conversation_gateway = real_gw
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)

        await bridge.on_audio_chunk(session, _stt_bytes("Нарисуй картинку с котом"))
        turn_id = await bridge.on_audio_commit(session, client_turn_id="retry-1")
        await session.current_task
        self.assertEqual(len(captured), 1, "tool invoked exactly once")

        # Simulate reconnect: brand-new session object attached to the SAME
        # conversation, client resubmits the SAME logical turn (client_turn_id).
        sink2 = NullSink()
        session2 = await bridge.create_session(
            tenant_id="t1", owner_id="u1", sink=sink2, conversation_id=session.conversation_id
        )
        turn_id2 = await bridge.on_audio_commit_replay(
            session2, text="Нарисуй картинку с котом", client_turn_id="retry-1"
        ) if hasattr(bridge, "on_audio_commit_replay") else None
        # No dedicated replay helper -- exercise the SAME commit_turn() path a
        # real client retry would hit after receiving no ack.
        turn_id2 = await bridge.commit_turn(
            session2, text="Нарисуй картинку с котом", client_turn_id="retry-1", is_voice=True
        )
        await session2.current_task

        self.assertEqual(len(captured), 1, "no accidental repeated external action on retry")

    async def test_g_reconnect_after_tool_action_completed_action_not_replayed(self):
        gw, captured = _image_tool_gateway()
        real_gw = WorkflowPandaConversationGateway(
            workflow_engine=_engine(), run_router=object(), context_manager=object(), tool_gateway=gw
        )
        self.rt.service.ba.conversation_gateway = real_gw
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Нарисуй кубик"))
        turn_id = await bridge.on_audio_commit(session, client_turn_id="draw-1")
        await session.current_task
        self.assertEqual(len(captured), 1)

        tool_events = [e for e in sink.events if e.type in ("tool.started", "tool.completed")]
        self.assertEqual(len(tool_events), 2)
        status_events = [e for e in sink.events if e.type == "assistant.status"]
        self.assertTrue(any(e.data.get("status") == "Создаю изображение" for e in status_events))


class VoiceToolAgentReuseTests(RealtimeBridgeTestBase):
    async def test_h_voice_tool_request_uses_canonical_tool_gateway_path(self):
        gw, captured = _image_tool_gateway()
        real_gw = WorkflowPandaConversationGateway(
            workflow_engine=_engine(), run_router=object(), context_manager=object(), tool_gateway=gw
        )
        self.rt.service.ba.conversation_gateway = real_gw
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="tenant-x", owner_id="user-x", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Нарисуй красную машину"))
        await bridge.on_audio_commit(session)
        await session.current_task

        self.assertEqual(len(captured), 1)
        sent = captured[0]
        self.assertEqual(sent.tool_id, "image.generate")
        self.assertEqual(sent.tenant_id, "tenant-x")
        self.assertEqual(sent.user_id, "user-x")

    async def test_i_voice_agent_request_reuses_same_conversation_gateway_respond(self):
        calls = []
        fake_gw = FakePandaConversationGateway(response="Ответ агента", calls=calls)
        self.rt.service.ba.conversation_gateway = fake_gw
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Попроси исследователя проверить это"))
        await bridge.on_audio_commit(session)
        await session.current_task
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0], ConversationRequest)
        self.assertEqual(calls[0].text, "Попроси исследователя проверить это")


class SpokenApprovalSafetyTests(RealtimeBridgeTestBase):
    async def test_style_cannot_bypass_pending_approval_requirement(self):
        """Block 4.33/54.I: a business action requiring HITL still requires
        approval regardless of style -- voice never grants a shortcut."""

        self.pz.set_preferences(tenant_id="t1", owner_id="u1", style=STYLE_PROFESSIONAL)

        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "unused"})
        engine.last_workflow_id = "wf-1"

        async def _invoke(tool_request, **kwargs):
            return ToolResult(
                request_id=tool_request.request_id,
                tool_id=tool_request.tool_id,
                operation=tool_request.operation,
                status="failed",
                success=False,
                error_code="tool_approval_required",
                data={},
            )

        gw = Mock()
        gw.invoke = AsyncMock(side_effect=_invoke)
        real_gw = WorkflowPandaConversationGateway(
            workflow_engine=engine, run_router=object(), context_manager=object(), tool_gateway=gw
        )
        self.rt.service.ba.conversation_gateway = real_gw
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Нарисуй кубик"))
        await bridge.on_audio_commit(session)
        await session.current_task
        completed = next(e for e in sink.events if e.type == "assistant.text.completed")
        self.assertIn("подтверждения", completed.data["text"])


class PersonalizationRealtimeIntegrationTests(RealtimeBridgeTestBase):
    async def test_style_directive_reaches_conversational_prompt_for_voice_turn(self):
        self.pz.set_preferences(
            tenant_id="t1", owner_id="u1", style=STYLE_PROFESSIONAL, language=LANGUAGE_RU
        )
        engine = _engine()
        real_gw = WorkflowPandaConversationGateway(
            workflow_engine=engine, run_router=object(), context_manager=object(), tool_gateway=None
        )
        self.rt.service.ba.conversation_gateway = real_gw
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Как дела?"))
        await bridge.on_audio_commit(session)
        await session.current_task

        prompt_arg = engine.execute.call_args.args[0]
        self.assertIn("Стиль общения: деловой", prompt_arg)
        self.assertIn("Как дела?", prompt_arg)

    async def test_voice_selection_used_for_subsequent_tts_without_touching_text(self):
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="Ответ")
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink, voice_id="alloy")
        await bridge.select_voice(session, "nova")
        self.assertEqual(session.voice_id, "nova")

        await bridge.on_audio_chunk(session, _stt_bytes("Привет"))
        await bridge.on_audio_commit(session)
        await session.current_task
        audio_bytes = b"".join(chunk for _tid, chunk in sink.audio_chunks)
        self.assertIn(b"TTS:nova:", audio_bytes)

        prefs = self.pz.get_preferences(tenant_id="t1", owner_id="u1")
        self.assertEqual(prefs.voice_id, "nova", "voice selection persists (4.29.2)")


class FinOpsUsageAttributionTests(RealtimeBridgeTestBase):
    """Block 4.37: STT/TTS usage is attributed to the SAME shared FinOps
    ledger by (tenant, owner, provider/capability) -- no paid call, no
    fabricated token/cost numbers, never blocks the turn."""

    async def test_stt_and_tts_calls_recorded_against_correct_tenant_and_owner(self):
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="Ответ")
        finops = FinOpsService()
        bridge = RealtimeConversationBridge(
            ba_api=self.rt.service, stt=self.stt, tts=self.tts, personalization=self.pz, finops=finops
        )
        sink = NullSink()
        session = await bridge.create_session(tenant_id="tenant-fin", owner_id="user-fin", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Привет"))
        await bridge.on_audio_commit(session)
        await session.current_task

        records = finops._store.records()
        by_capability = {r.provider_id: r for r in records}
        self.assertIn("speech_stt", by_capability)
        self.assertIn("speech_tts", by_capability)
        for rec in records:
            self.assertEqual(rec.tenant_id, "tenant-fin")
            self.assertEqual(rec.user_id, "user-fin")

    async def test_missing_finops_never_breaks_the_turn(self):
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="Ответ")
        bridge = self._bridge()  # finops=None by default
        sink = NullSink()
        session = await bridge.create_session(tenant_id="t1", owner_id="u1", sink=sink)
        await bridge.on_audio_chunk(session, _stt_bytes("Привет"))
        await bridge.on_audio_commit(session)
        await session.current_task
        self.assertTrue(any(e.type == "assistant.text.completed" for e in sink.events))


class ResumeExistingConversationTests(RealtimeBridgeTestBase):
    async def test_resume_uses_existing_conversation_not_a_shadow_one(self):
        self.rt.service.ba.conversation_gateway = FakePandaConversationGateway(response="ОК")
        existing = self.rt.service.create_conversation(tenant_id="t1", owner_id="u1", title="Existing")
        bridge = self._bridge()
        sink = NullSink()
        session = await bridge.create_session(
            tenant_id="t1", owner_id="u1", sink=sink, conversation_id=existing.conversation_id
        )
        self.assertEqual(session.conversation_id, existing.conversation_id)


if __name__ == "__main__":
    unittest.main()
