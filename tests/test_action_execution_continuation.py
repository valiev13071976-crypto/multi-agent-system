"""Deterministic action execution + multi-turn continuation (no real providers)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from autonomy.capabilities import CAP_IMAGE_GENERATE, CapabilitySet
from autonomy.models import ACTION_READ
from business_assistant.action_continuation import (
    ANSWER_TEXT,
    ASK_CLARIFICATION,
    CALL_TOOL,
    CONTINUE_ACTIVE_TASK,
    FAIL_UNAVAILABLE,
    FAMILY_IMAGE_GENERATE,
    NEW_TASK,
    PARAM_QUANTITY,
    READY_TO_EXECUTE,
    REQUEST_APPROVAL,
    ActiveTask,
    ActiveTaskStore,
    continuation_decision,
    resolve_action_turn,
    scene_mentions,
)
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
    select_canonical_final_answer,
)
from business_assistant.follow_up import KIND_TRANSFORM, HistoryTurn, resolve_follow_up
from business_assistant.intent import classify_intent
from business_assistant.models import INTENT_CONVERSATIONAL
from business_assistant.service import BusinessAssistantService
from agents.execution_policy import (
    POLICY_FULL,
    POLICY_LIGHTWEIGHT,
    POLICY_STANDARD,
    resolve_execution_policy,
)
from agents.response_depth import classify_response_depth
from agents.routing_requirements import FRESHNESS_CURRENT, derive_task_requirements
from agents.task_classifier import CATEGORY_GENERAL, TaskClassifier
from tools.gateway import ToolGateway
from tools.models import (
    RETRY_NONE,
    SIDE_EFFECT_NONE,
    TOOL_TRUST_INTERNAL_SAFE,
    ToolDescriptor,
    ToolResult,
    TOOL_STATUS_SUCCEEDED,
)
from tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[1]
WOLF = "сделай мне картинку с волком"


class FakeImageGenerateAdapter:
    adapter_id = "image"

    def __init__(self):
        self.calls = []

    def supports(self, tool_id: str) -> bool:
        return tool_id in {"image.generate", "image.edit"}

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY

        return ADAPTER_HEALTHY

    async def execute_read(self, request, context) -> dict:
        self.calls.append(dict(request.arguments or {}))
        args = dict(request.arguments or {})
        n = int(args.get("variant_count") or 1)
        return {
            "version_ids": [f"img-{len(self.calls)}-{i}" for i in range(n)],
            "mime_type": "image/png",
            "artifact_kind": "image",
            "status": "completed",
            "view_url": "/media/fake-image.png",
            "provenance": {"adapter": "fake-image", "fake": True},
        }


def _image_descriptor():
    return ToolDescriptor(
        tool_id="image.generate",
        name="Image Generate",
        description="Test image generation",
        version="1.0.0",
        trust_level=TOOL_TRUST_INTERNAL_SAFE,
        capabilities_required=(CAP_IMAGE_GENERATE,),
        action_types_supported=(ACTION_READ,),
        operations=("generate",),
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=15.0,
        enabled=True,
        network_access=False,
        category="image",
        adapter_id="image",
        side_effect_level=SIDE_EFFECT_NONE,
        retry_policy=RETRY_NONE,
    )


def _image_gateway(adapter=None):
    adapter = adapter or FakeImageGenerateAdapter()
    registry = ToolRegistry()
    registry.register(_image_descriptor(), adapter=adapter)
    return ToolGateway(registry=registry, register_search=False), adapter


def _caps():
    return CapabilitySet(
        subject_id="u1",
        capabilities=(CAP_IMAGE_GENERATE,),
        issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _store() -> ActiveTaskStore:
    return ActiveTaskStore()


def _resolve(text: str, store: ActiveTaskStore, *, gateway=None, conv="c1", tenant="tenant-a", owner="u1", req="r1"):
    return resolve_action_turn(
        text,
        tenant_id=tenant,
        owner_id=owner,
        conversation_id=conv,
        store=store,
        follow_up=resolve_follow_up(text, ()),
        gateway=gateway,
        request_id=req,
    )


def _engine_gateway(*, tool_gateway=None, execute_value=None, adapter=None):
    engine = Mock()
    engine.execute = AsyncMock(
        return_value={"final_answer": execute_value or "Текстовый ответ.", "role": "Judge"}
    )
    engine.last_workflow_id = "wf-1"
    if tool_gateway is None and adapter is None:
        gw_tool, adapter = _image_gateway()
    elif tool_gateway is None:
        gw_tool, adapter = _image_gateway(adapter)
    else:
        gw_tool = tool_gateway
    gw = WorkflowPandaConversationGateway(
        workflow_engine=engine,
        run_router=object(),
        context_manager=object(),
        tool_gateway=gw_tool,
        tool_capabilities=_caps(),
    )
    return gw, engine, adapter


def _req(text: str, *, conv="c1", req="r1", tenant="tenant-a", user="u1", history=()):
    return ConversationRequest(
        text=text,
        tenant_id=tenant,
        user_id=user,
        request_id=req,
        conversation_id=conv,
        history=history,
    )


class ContinuationResolverTests(unittest.TestCase):
    def test_direct_image_request_selects_capability(self):
        gw, adapter = _image_gateway()
        store = _store()
        decision = _resolve(WOLF, store, gateway=gw)
        self.assertEqual(decision.decision, CALL_TOOL)
        self.assertEqual(decision.readiness, READY_TO_EXECUTE)
        self.assertEqual(decision.task.family, FAMILY_IMAGE_GENERATE)
        self.assertFalse(decision.extra_llm)
        self.assertTrue(scene_mentions(decision.task, "волк"))

    def test_no_over_clarify_when_scene_present(self):
        gw, _ = _image_gateway()
        decision = _resolve(WOLF, _store(), gateway=gw)
        self.assertEqual(decision.decision, CALL_TOOL)
        self.assertNotEqual(decision.decision, ASK_CLARIFICATION)
        self.assertNotIn("стиль", (decision.user_message or "").casefold())

    def test_optional_aspect_uses_default(self):
        gw, _ = _image_gateway()
        decision = _resolve(WOLF, _store(), gateway=gw)
        self.assertEqual(decision.arguments.get("aspect_ratio"), "1:1")
        self.assertEqual(int(decision.arguments.get("variant_count") or 0), 1)

    def test_missing_scene_asks_one_clarification(self):
        gw, _ = _image_gateway()
        decision = _resolve("сгенерируй изображение", _store(), gateway=gw)
        self.assertEqual(decision.decision, ASK_CLARIFICATION)
        self.assertEqual(decision.user_message.count("?"), 1)

    def test_clarification_binds_to_active_task(self):
        gw, _ = _image_gateway()
        store = _store()
        first = _resolve("сгенерируй изображение", store, gateway=gw)
        self.assertEqual(first.decision, ASK_CLARIFICATION)
        second = _resolve("волка в лесу ночь", store, gateway=gw, req="r2")
        self.assertEqual(second.task.task_id, first.task.task_id)
        self.assertTrue(scene_mentions(second.task, "волк"))

    def test_numeric_follow_up_binds_quantity(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve(WOLF, store, gateway=gw)
        _resolve("сделай несколько", store, gateway=gw, req="r2")
        third = _resolve("2", store, gateway=gw, req="r3")
        self.assertEqual(third.task.quantity, 2)
        self.assertEqual(int(third.arguments.get(PARAM_QUANTITY) or 0), 2)

    def test_neskolko_keeps_artifact_context(self):
        gw, _ = _image_gateway()
        store = _store()
        first = _resolve(WOLF, store, gateway=gw)
        second = _resolve("сделай несколько", store, gateway=gw, req="r2")
        self.assertEqual(second.continuation, CONTINUE_ACTIVE_TASK)
        self.assertEqual(second.task.task_id, first.task.task_id)
        self.assertTrue(scene_mentions(second.task, "волк"))
        self.assertEqual(second.decision, ASK_CLARIFICATION)

    def test_short_noun_refines_style(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve(WOLF, store, gateway=gw)
        decision = _resolve("логотип", store, gateway=gw, req="r2")
        self.assertEqual(decision.task.parameters.get("style"), "logo")
        self.assertTrue(scene_mentions(decision.task, "волк"))

    def test_short_adjective_refines_style(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve(WOLF, store, gateway=gw)
        decision = _resolve("реализм", store, gateway=gw, req="r2")
        self.assertEqual(decision.task.parameters.get("style"), "realistic")

    def test_explicit_correction_replaces_time(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve("сделай картинку волка в лесу ночью", store, gateway=gw)
        decision = _resolve("нет, днём", store, gateway=gw, req="r2")
        self.assertTrue(scene_mentions(decision.task, "day") or scene_mentions(decision.task, "дн"))
        blob = str(decision.task.parameters.get("scene_description") or "").casefold()
        self.assertNotIn("ноч", blob)

    def test_quantity_correction(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve(WOLF, store, gateway=gw)
        _resolve("2 варианта", store, gateway=gw, req="r2")
        decision = _resolve("нет, 3", store, gateway=gw, req="r3")
        self.assertEqual(decision.task.quantity, 3)

    def test_execute_verb_uses_accumulated_task(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve(WOLF, store, gateway=gw)
        _resolve("логотип", store, gateway=gw, req="r2")
        _resolve("сделай несколько", store, gateway=gw, req="r3")
        _resolve("2", store, gateway=gw, req="r4")
        _resolve("волка в лесу ночь", store, gateway=gw, req="r5")
        final = _resolve("сгенерируй", store, gateway=gw, req="r6")
        self.assertEqual(final.decision, CALL_TOOL)
        self.assertEqual(final.task.quantity, 2)
        self.assertTrue(scene_mentions(final.task, "волк"))
        self.assertTrue(scene_mentions(final.task, "лес") or scene_mentions(final.task, "forest"))
        self.assertTrue(scene_mentions(final.task, "ноч") or scene_mentions(final.task, "night"))
        self.assertEqual(final.task.parameters.get("style"), "logo")

    def test_typo_does_not_need_exact_keyword(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve(WOLF, store, gateway=gw)
        decision = _resolve("сгененрировать", store, gateway=gw, req="r2")
        self.assertEqual(decision.decision, CALL_TOOL)
        img = _resolve("изоброжение", _store(), gateway=gw)
        self.assertIn(img.decision, {CALL_TOOL, ASK_CLARIFICATION, FAIL_UNAVAILABLE})
        self.assertEqual(img.task.family, FAMILY_IMAGE_GENERATE)

    def test_unrelated_weather_is_new_task(self):
        gw, _ = _image_gateway()
        store = _store()
        first = _resolve(WOLF, store, gateway=gw)
        decision = _resolve("А какая завтра погода в Москве?", store, gateway=gw, req="r2")
        self.assertEqual(decision.continuation, NEW_TASK)
        self.assertEqual(decision.decision, ANSWER_TEXT)
        stored = store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")
        self.assertEqual(stored.status, "SUPERSEDED")
        self.assertEqual(first.task.family, FAMILY_IMAGE_GENERATE)

    def test_cancel_does_not_execute(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve("сгенерируй изображение", store, gateway=gw)
        decision = _resolve("отмена", store, gateway=gw, req="r2")
        self.assertEqual(decision.task.status, "CANCELLED")
        self.assertNotEqual(decision.decision, CALL_TOOL)

    def test_yes_does_not_bypass_hitl_write(self):
        store = _store()
        task = ActiveTask(
            task_id="t-write",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            family="write_governed",
            tool_id="commerce.price.apply",
            operation="apply",
            goal="изменить цену",
            status="WAITING_FOR_INPUT",
            risk="write_governed",
        )
        store.put(task)
        decision = _resolve("да", store, gateway=_image_gateway()[0])
        self.assertEqual(decision.decision, REQUEST_APPROVAL)
        self.assertNotEqual(decision.decision, CALL_TOOL)

    def test_write_phrase_is_not_auto_executed(self):
        decision = _resolve("измени цену товара", _store(), gateway=_image_gateway()[0])
        self.assertNotEqual(decision.decision, CALL_TOOL)
        self.assertEqual(decision.readiness, "NEEDS_APPROVAL")

    def test_cross_tenant_task_isolation(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve(WOLF, store, gateway=gw, tenant="tenant-a")
        other = store.get(tenant_id="tenant-b", owner_id="u1", conversation_id="c1")
        self.assertIsNone(other)
        own = store.get(tenant_id="tenant-a", owner_id="u2", conversation_id="c1")
        self.assertIsNone(own)

    def test_freshness_not_bypassed(self):
        req = derive_task_requirements(category="general", text="найди текущую комиссию Ozon")
        self.assertEqual(req.freshness, FRESHNESS_CURRENT)
        decision = _resolve("найди текущую комиссию Ozon", _store(), gateway=_image_gateway()[0])
        self.assertEqual(decision.decision, ANSWER_TEXT)
        self.assertNotEqual(decision.decision, CALL_TOOL)

    def test_follow_up_transform_preserved(self):
        history = (
            HistoryTurn("user", "суп из петуха"),
            HistoryTurn("assistant", "Варить долго."),
            HistoryTurn("user", "пометь главное"),
        )
        res = resolve_follow_up("пометь главное", history)
        self.assertEqual(res.kind, KIND_TRANSFORM)
        decision = resolve_action_turn(
            "пометь главное",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            store=_store(),
            follow_up=res,
            gateway=_image_gateway()[0],
            request_id="r1",
        )
        self.assertEqual(decision.decision, ANSWER_TEXT)

    def test_unavailable_capability_not_faked(self):
        decision = _resolve(WOLF, _store(), gateway=None)
        self.assertEqual(decision.decision, FAIL_UNAVAILABLE)
        self.assertIn("недоступ", decision.user_message.casefold())
        self.assertNotIn("промпт", decision.user_message.casefold())

    def test_policies_unchanged(self):
        clf = TaskClassifier().classify("привет")
        self.assertEqual(clf.category, CATEGORY_GENERAL)
        self.assertEqual(
            resolve_execution_policy(
                category="general",
                response_depth=classify_response_depth("привет", category="general"),
                requirements=clf.requirements,
            ),
            POLICY_LIGHTWEIGHT,
        )
        self.assertEqual(
            resolve_execution_policy(
                category="general",
                response_depth="analytical",
                requirements=clf.requirements,
            ),
            POLICY_STANDARD,
        )
        req = derive_task_requirements(category="research", text="проверь факты")
        self.assertEqual(
            resolve_execution_policy(
                category="research",
                response_depth="deep",
                requirements=req,
            ),
            POLICY_FULL,
        )


class ActionGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_wolf_invokes_mocked_image_tool(self):
        gw, engine, adapter = _engine_gateway()
        result = await gw.respond(_req(WOLF))
        self.assertEqual(len(adapter.calls), 1)
        engine.execute.assert_not_called()
        self.assertIn("готов", result.text.casefold())
        self.assertNotIn("промпт", result.text.casefold())
        self.assertNotIn("вставьте", result.text.casefold())
        self.assertTrue(result.metadata.get("artifacts"))
        self.assertNotIn("CALL_TOOL", result.text)
        self.assertNotIn("image.generate", result.text)

    async def test_logo_refinement_does_not_duplicate_execution(self):
        gw, engine, adapter = _engine_gateway()
        await gw.respond(_req(WOLF, req="w1"))
        await gw.respond(_req("логотип", req="w2"))
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(gw.last_action_decision.task.parameters.get("style"), "logo")

    async def test_tool_executes_at_most_once_per_request(self):
        gw, engine, adapter = _engine_gateway()
        first = await gw.respond(_req(WOLF, req="same-key"))
        second = await gw.respond(_req(WOLF, req="same-key"))
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(second.metadata.get("duplicate") or first.metadata.get("artifacts"))

    async def test_sequence_retains_task_and_executes(self):
        gw, engine, adapter = _engine_gateway()
        turns = [
            (WOLF, "a"),
            ("логотип", "b"),
            ("сделай несколько", "c"),
            ("2", "d"),
            ("волка в лесу ночь", "e"),
            ("сгенерируй", "f"),
        ]
        last = None
        for text, rid in turns:
            last = await gw.respond(_req(text, req=rid))
        self.assertEqual(gw.last_action_decision.decision, CALL_TOOL)
        task = gw.last_action_decision.task
        self.assertEqual(task.quantity, 2)
        self.assertTrue(scene_mentions(task, "волк"))
        self.assertTrue(scene_mentions(task, "лес") or scene_mentions(task, "forest"))
        self.assertTrue(scene_mentions(task, "ноч") or scene_mentions(task, "night"))
        self.assertGreaterEqual(len(adapter.calls), 1)
        self.assertNotIn("несколько чего", (last.text or "").casefold())

    async def test_tool_error_is_user_safe_and_preserves_chat(self):
        class Boom(FakeImageGenerateAdapter):
            async def execute_read(self, request, context):
                raise RuntimeError("secret stack openai_api_key=sk-test")

        gw, engine, adapter = _engine_gateway(adapter=Boom())
        result = await gw.respond(_req(WOLF))
        self.assertNotIn("Traceback", result.text)
        self.assertNotIn("sk-test", result.text)
        self.assertNotIn("RuntimeError", result.text)
        stored = gw._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")
        self.assertIsNotNone(stored)

    async def test_canonical_text_path_still_uses_engine(self):
        gw, engine, adapter = _engine_gateway()
        history = (
            HistoryTurn("user", "суп из петуха хочу сделать"),
            HistoryTurn("assistant", "Петуха варить долго."),
            HistoryTurn("user", "пометь главное"),
        )
        result = await gw.respond(_req("пометь главное", history=history, req="t-follow"))
        engine.execute.assert_called_once()
        self.assertEqual(adapter.calls, [])
        self.assertEqual(result.text, "Текстовый ответ.")

    async def test_greeting_not_an_image_task(self):
        gw, engine, adapter = _engine_gateway()
        await gw.respond(_req("привет", req="hi"))
        engine.execute.assert_called_once()
        self.assertEqual(adapter.calls, [])

    async def test_artifacts_flow_to_business_execution(self):
        gw, engine, adapter = _engine_gateway()
        ba = BusinessAssistantService(conversation_gateway=gw)
        req = ba.submit_request(tenant_id="tenant-a", user_id="u1", text=WOLF)
        self.assertEqual(req.intent, INTENT_CONVERSATIONAL)
        ex = await ba.respond_conversationally_async(
            request_id=req.request_id,
            tenant_id="tenant-a",
            conversation_id="c1",
        )
        self.assertTrue(ex.artifacts)
        self.assertEqual(ex.mode, "CONVERSATIONAL")
        result = ba.get_result(execution_id=ex.execution_id, tenant_id="tenant-a")
        self.assertTrue(result["artifacts"])
        self.assertNotIn("CALL_TOOL", result["summary"])

    async def test_no_duplicate_assistant_text_from_tool(self):
        gw, _, _ = _engine_gateway()
        result = await gw.respond(_req(WOLF))
        canonical = select_canonical_final_answer({"final_answer": result.text, "summary": result.text})
        self.assertEqual(canonical, result.text)

    async def test_owner_isolation(self):
        gw, _, adapter = _engine_gateway()
        await gw.respond(_req(WOLF, user="u1"))
        other = gw._action_store.get(tenant_id="tenant-a", owner_id="u2", conversation_id="c1")
        self.assertIsNone(other)
        self.assertEqual(len(adapter.calls), 1)


class FrontendArtifactTests(unittest.TestCase):
    def test_markdown_image_renderer_present(self):
        src = (ROOT / "static/panda/js/sanitize.js").read_text(encoding="utf-8")
        self.assertIn("createElement(\"img\")", src)
        self.assertIn("img.src", src)

    def test_artifacts_do_not_dump_json(self):
        src = (ROOT / "static/panda/js/components.js").read_text(encoding="utf-8")
        self.assertNotIn("JSON.stringify(a)", src)
        self.assertIn("artifact-item", src)

    def test_internal_labels_not_in_copy(self):
        app = (ROOT / "static/panda/js/app.js").read_text(encoding="utf-8")
        self.assertNotIn("CALL_TOOL", app)
        self.assertNotIn("ActiveTask", app)
        self.assertNotIn("image.generate", app)


class ConversationalIntentTests(unittest.TestCase):
    def test_wolf_is_conversational_not_business_write(self):
        self.assertEqual(classify_intent(WOLF), INTENT_CONVERSATIONAL)
        self.assertNotEqual(classify_intent("измени цену товара на Ozon"), INTENT_CONVERSATIONAL)


class ContinuationAmbiguityTests(unittest.TestCase):
    def test_numeric_without_task_is_not_forced(self):
        decision = continuation_decision("2", active=None)
        self.assertEqual(decision, NEW_TASK)

    def test_ambiguous_short_unrelated(self):
        gw, _ = _image_gateway()
        store = _store()
        _resolve(WOLF, store, gateway=gw)
        # a clearly new long question supersedes
        decision = _resolve("Расскажи что такое налог для ИП", store, gateway=gw, req="r2")
        self.assertEqual(decision.continuation, NEW_TASK)


if __name__ == "__main__":
    unittest.main()
