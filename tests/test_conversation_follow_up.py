"""Deterministic conversation follow-up / context resolution (no real providers)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from agents.routing_requirements import FRESHNESS_CURRENT, FRESHNESS_HISTORICAL, derive_task_requirements
from agents.task_classifier import CATEGORY_GENERAL, ROLE_GENERALIST, TaskClassifier
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
    is_internal_assistant_text,
)
from business_assistant.follow_up import (
    KIND_MISSING_CONTEXT,
    KIND_NEW_TOPIC,
    KIND_QUESTION,
    KIND_REFERENT,
    KIND_TRANSFORM,
    TARGET_PREVIOUS_ASSISTANT,
    TARGET_PREVIOUS_OPTIONS,
    TARGET_PREVIOUS_TOPIC,
    HistoryTurn,
    build_follow_up_prompt,
    resolve_follow_up,
)
from business_assistant_api.models import ConversationRecord, MessageRecord
from business_assistant_api.runtime import build_business_assistant_api_runtime
from business_assistant_api.service import BusinessAssistantApiService


RECIPE_USER = "суп из петуха хочу сделать"
RECIPE_ASSISTANT = (
    "Петуха варить на слабом огне 2,5–4 часа. Снимать пену. "
    "Процедить бульон. Картофель 10–12 минут, затем лапша."
)
OPTIONS_ASSISTANT = "1. Ozon\n2. Wildberries\n3. Yandex Market"
JUDGE_META = (
    "Синтез ответов экспертов без скрытого приоритета provider. "
    "Внешняя проверка фактов учитывается только при независимых источниках."
)


def _recipe_history(current: str) -> tuple[HistoryTurn, ...]:
    return (
        HistoryTurn("user", RECIPE_USER),
        HistoryTurn("assistant", RECIPE_ASSISTANT),
        HistoryTurn("user", current),
    )


class FollowUpResolverTests(unittest.TestCase):
    def test_a_highlight_main_uses_previous_assistant(self):
        current = "пометь главное"
        res = resolve_follow_up(current, _recipe_history(current))
        self.assertEqual(res.kind, KIND_TRANSFORM)
        self.assertEqual(res.target, TARGET_PREVIOUS_ASSISTANT)
        self.assertTrue(res.inject_context)
        prompt = build_follow_up_prompt(current, res)
        self.assertIn(RECIPE_ASSISTANT, prompt)
        self.assertIn("Do not ask them to resend the text", prompt)
        self.assertNotIn("пришлите текст", prompt.casefold())

    def test_b_make_shorter(self):
        current = "сделай короче"
        res = resolve_follow_up(current, _recipe_history(current))
        self.assertEqual(res.kind, KIND_TRANSFORM)
        self.assertIn(RECIPE_ASSISTANT, build_follow_up_prompt(current, res))

    def test_c_salt_question(self):
        current = "а сколько соли?"
        res = resolve_follow_up(current, _recipe_history(current))
        self.assertEqual(res.kind, KIND_QUESTION)
        self.assertEqual(res.target, TARGET_PREVIOUS_TOPIC)
        self.assertIn(RECIPE_ASSISTANT, build_follow_up_prompt(current, res))

    def test_d_second_referent(self):
        current = "а второй?"
        history = (
            HistoryTurn("user", "куда выходить"),
            HistoryTurn("assistant", OPTIONS_ASSISTANT),
            HistoryTurn("user", current),
        )
        res = resolve_follow_up(current, history)
        self.assertEqual(res.kind, KIND_REFERENT)
        self.assertEqual(res.target, TARGET_PREVIOUS_OPTIONS)
        prompt = build_follow_up_prompt(current, res)
        self.assertIn("Wildberries", prompt)

    def test_e_why(self):
        current = "почему?"
        res = resolve_follow_up(current, _recipe_history(current))
        self.assertEqual(res.kind, KIND_QUESTION)
        self.assertTrue(res.inject_context)

    def test_f_missing_context_fallback(self):
        current = "сделай короче"
        res = resolve_follow_up(current, ())
        self.assertEqual(res.kind, KIND_MISSING_CONTEXT)
        prompt = build_follow_up_prompt(current, res)
        self.assertIn("no previous assistant answer", prompt.casefold())
        self.assertNotIn(RECIPE_ASSISTANT, prompt)

    def test_g_new_topic_not_recipe(self):
        current = "Теперь сравни Ozon и Wildberries для продажи электроники."
        res = resolve_follow_up(current, _recipe_history(current))
        self.assertEqual(res.kind, KIND_NEW_TOPIC)
        self.assertFalse(res.inject_context)
        prompt = build_follow_up_prompt(current, res)
        self.assertEqual(prompt, current)
        self.assertNotIn(RECIPE_ASSISTANT, prompt)

    def test_h_current_commission_freshness(self):
        current = "а сколько сейчас комиссия Ozon?"
        res = resolve_follow_up(current, _recipe_history(current))
        self.assertEqual(res.kind, KIND_NEW_TOPIC)
        req = derive_task_requirements(category="general", text=current)
        self.assertEqual(req.freshness, FRESHNESS_CURRENT)

    def test_i_historical_commission(self):
        current = "а какая комиссия была в 2024?"
        req = derive_task_requirements(category="general", text=current)
        self.assertEqual(req.freshness, FRESHNESS_HISTORICAL)

    def test_j_one_sentence_transform_compact(self):
        current = "ответь одним предложением"
        res = resolve_follow_up(current, _recipe_history(current))
        self.assertEqual(res.kind, KIND_TRANSFORM)
        clf = TaskClassifier().classify(current)
        self.assertEqual(clf.category, CATEGORY_GENERAL)
        self.assertEqual(clf.role_id, ROLE_GENERALIST)

    def test_n_internal_metadata_skipped_as_assistant_context(self):
        self.assertTrue(is_internal_assistant_text(JUDGE_META))
        history = (
            HistoryTurn("user", RECIPE_USER),
            HistoryTurn("assistant", JUDGE_META),
            HistoryTurn("assistant", RECIPE_ASSISTANT),
            HistoryTurn("user", "пометь главное"),
        )
        res = resolve_follow_up("пометь главное", history)
        self.assertEqual(res.previous_assistant, RECIPE_ASSISTANT)
        self.assertNotIn("без скрытого приоритета", res.previous_assistant.casefold())
        only_meta = (
            HistoryTurn("user", RECIPE_USER),
            HistoryTurn("assistant", JUDGE_META),
            HistoryTurn("user", "пометь главное"),
        )
        res_meta = resolve_follow_up("пометь главное", only_meta)
        self.assertEqual(res_meta.kind, KIND_MISSING_CONTEXT)

    def test_p_standalone_weather_unchanged(self):
        current = "Какая погода в Москве?"
        res = resolve_follow_up(current, _recipe_history(current))
        self.assertFalse(res.inject_context)
        self.assertEqual(build_follow_up_prompt(current, res), current)


class FollowUpGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_executes_resolved_prompt(self):
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "Главное: варить долго.", "role": "Judge"})
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
        )
        current = "пометь главное"
        await gw.respond(
            ConversationRequest(
                text=current,
                tenant_id="t1",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                history=_recipe_history(current),
            )
        )
        prompt = engine.execute.await_args.args[0]
        self.assertIn(RECIPE_ASSISTANT, prompt)
        self.assertNotIn("пришлите текст", prompt.casefold())


class FollowUpIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        rt = build_business_assistant_api_runtime(
            env={"BA_API_DB_PATH": os.path.join(self.tmpdir, "ba.sqlite")},
            db_path=os.path.join(self.tmpdir, "ba.sqlite"),
            with_integration=False,
        )
        self.addCleanup(rt.close)
        self.svc: BusinessAssistantApiService = rt.service
        now = "2026-09-04T00:00:00+00:00"
        self.svc.store.save_conversation(
            ConversationRecord("conv-a", "tenant-a", "owner-a", now, now, {})
        )
        self.svc.store.save_conversation(
            ConversationRecord("conv-b", "tenant-b", "owner-b", now, now, {})
        )
        self.svc.store.save_conversation(
            ConversationRecord("conv-a2", "tenant-a", "owner-x", now, now, {})
        )
        self.svc.store.save_message(
            MessageRecord("m1", "conv-a", "tenant-a", "assistant", RECIPE_ASSISTANT, now)
        )
        self.svc.store.save_message(
            MessageRecord("m2", "conv-b", "tenant-b", "assistant", "SECRET-B", now)
        )
        self.svc.store.save_message(
            MessageRecord("m3", "conv-a2", "tenant-a", "assistant", "SECRET-OWNER", now)
        )

    def test_k_tenant_isolation(self):
        turns = self.svc._history_turns(tenant="tenant-a", owner_id="owner-a", conversation_id="conv-b")
        self.assertEqual(turns, ())

    def test_l_owner_isolation(self):
        turns = self.svc._history_turns(tenant="tenant-a", owner_id="owner-a", conversation_id="conv-a2")
        self.assertEqual(turns, ())

    def test_m_conversation_isolation(self):
        self.svc.store.save_conversation(
            ConversationRecord(
                "conv-c", "tenant-a", "owner-a", "2026-09-04T00:00:01+00:00", "2026-09-04T00:00:01+00:00", {}
            )
        )
        turns = self.svc._history_turns(tenant="tenant-a", owner_id="owner-a", conversation_id="conv-c")
        self.assertEqual(turns, ())
        own = self.svc._history_turns(tenant="tenant-a", owner_id="owner-a", conversation_id="conv-a")
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0].content, RECIPE_ASSISTANT)
        self.assertNotIn("SECRET-B", own[0].content)
        self.assertNotIn("SECRET-OWNER", own[0].content)


if __name__ == "__main__":
    unittest.main()
