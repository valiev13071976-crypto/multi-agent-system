"""Focused tests — conversational Panda AI path vs business workflow (Web BA path)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from business_assistant.conversation_gateway import FakePandaConversationGateway
from business_assistant.errors import BA_CONVERSATION_UNAVAILABLE, BusinessAssistantError
from business_assistant.intent import classify_intent, is_conversational
from business_assistant.models import INTENT_CONVERSATIONAL, INTENT_UPDATE
from business_assistant.service import BusinessAssistantService
from business_assistant_api.errors import BAA_CONVERSATION_UNAVAILABLE, BusinessAssistantApiError
from business_assistant_api.models import ST_COMPLETED, ST_FAILED
from business_assistant_api.runtime import build_business_assistant_api_runtime, wire_panda_conversation_gateway
from integrations.activation.models import ENV_FIXTURE, ENV_LIVE
from integrations.activation.service import IntegrationActivationService

FAKE_REPLY = "Panda intelligence response"
WORKFLOW_DIAGNOSTICS = (
    "Requested:",
    "Findings:",
    "Artifacts:",
    "Fixture_mode:",
    "Approved:",
    "Waiting_approval:",
)


def _fake_gateway(calls=None):
    return FakePandaConversationGateway(response=FAKE_REPLY, calls=calls if calls is not None else [])


class IntentRoutingTests(unittest.TestCase):
    def test_greeting_is_conversational(self):
        self.assertTrue(is_conversational("привет как ты"))
        self.assertEqual(classify_intent("привет как ты"), INTENT_CONVERSATIONAL)

    def test_general_education_is_conversational(self):
        text = "Объясни мне НДС простыми словами"
        self.assertTrue(is_conversational(text))
        self.assertEqual(classify_intent(text), INTENT_CONVERSATIONAL)

    def test_business_reasoning_is_conversational(self):
        text = "Сравни плюсы и минусы продажи на своем сайте и маркетплейсе"
        self.assertTrue(is_conversational(text))
        self.assertEqual(classify_intent(text), INTENT_CONVERSATIONAL)

    def test_ozon_explanation_not_integration(self):
        text = "Объясни, как работает комиссия Ozon"
        self.assertTrue(is_conversational(text))
        self.assertEqual(classify_intent(text), INTENT_CONVERSATIONAL)

    def test_ozon_data_request_is_business(self):
        text = "Покажи мою текущую комиссию Ozon"
        self.assertFalse(is_conversational(text))
        self.assertNotEqual(classify_intent(text), INTENT_CONVERSATIONAL)

    def test_1c_risks_discussion_not_integration(self):
        text = "Какие риски есть при синхронизации 1С и сайта?"
        self.assertTrue(is_conversational(text))
        self.assertEqual(classify_intent(text), INTENT_CONVERSATIONAL)

    def test_1c_stock_request_is_business(self):
        text = "Покажи остаток товара ABC из моей 1С"
        self.assertFalse(is_conversational(text))
        self.assertNotEqual(classify_intent(text), INTENT_CONVERSATIONAL)

    def test_ozon_price_check_not_conversational(self):
        text = "Проверь цену на Ozon для Samsung"
        self.assertFalse(is_conversational(text))

    def test_write_request_not_conversational(self):
        text = "Измени цену SKU-123 на Ozon на 49990"
        self.assertFalse(is_conversational(text))
        self.assertEqual(classify_intent(text), INTENT_UPDATE)


class BusinessAssistantConversationalTests(unittest.TestCase):
    def test_gateway_invoked_without_integration(self):
        calls: list = []
        gateway = _fake_gateway(calls)
        ba = BusinessAssistantService(integration_environment=ENV_FIXTURE, conversation_gateway=gateway)
        req = ba.submit_request(tenant_id="tenant-a", user_id="user-a", text="привет")
        self.assertEqual(req.intent, INTENT_CONVERSATIONAL)
        with patch.object(ba, "resolve_integration") as mock_resolve:
            ex = ba.respond_conversationally(
                request_id=req.request_id,
                tenant_id="tenant-a",
                conversation_id="conv-ctx",
            )
            mock_resolve.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].tenant_id, "tenant-a")
        self.assertEqual(calls[0].user_id, "user-a")
        self.assertEqual(calls[0].conversation_id, "conv-ctx")
        self.assertEqual(ex.mode, "CONVERSATIONAL")
        self.assertEqual(ex.summary, FAKE_REPLY)
        for marker in WORKFLOW_DIAGNOSTICS:
            self.assertNotIn(marker, ex.summary)

    def test_general_knowledge_uses_gateway(self):
        gateway = _fake_gateway()
        ba = BusinessAssistantService(conversation_gateway=gateway)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Объясни мне НДС простыми словами",
        )
        ex = ba.respond_conversationally(request_id=req.request_id, tenant_id="tenant-a")
        self.assertEqual(ex.summary, FAKE_REPLY)

    def test_missing_gateway_fails_closed(self):
        ba = BusinessAssistantService(integration_environment=ENV_FIXTURE, conversation_gateway=None)
        req = ba.submit_request(tenant_id="tenant-a", user_id="u", text="привет")
        with self.assertRaises(BusinessAssistantError) as ctx:
            ba.respond_conversationally(request_id=req.request_id, tenant_id="tenant-a")
        self.assertEqual(ctx.exception.code, BA_CONVERSATION_UNAVAILABLE)

    def test_conversational_independent_of_integration_env(self):
        for env in (ENV_FIXTURE, ENV_LIVE):
            gateway = _fake_gateway()
            ba = BusinessAssistantService(
                integration_environment=env,
                conversation_gateway=gateway,
            )
            req = ba.submit_request(tenant_id="tenant-a", user_id="u", text="привет")
            ex = ba.respond_conversationally(request_id=req.request_id, tenant_id="tenant-a")
            self.assertEqual(ex.summary, FAKE_REPLY)

    def test_fixture_env_unchanged_for_business(self):
        activation = IntegrationActivationService()
        ba = BusinessAssistantService(
            integration_activation=activation,
            integration_environment=ENV_FIXTURE,
            conversation_gateway=_fake_gateway(),
        )
        self.assertEqual(ba.integration_environment, ENV_FIXTURE)
        req = ba.submit_request(tenant_id="tenant-a", user_id="u", text="Проверь цену на Ozon")
        self.assertNotEqual(req.intent, INTENT_CONVERSATIONAL)
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        self.assertGreater(len(plan.steps), 0)

    def test_write_request_stays_business_workflow(self):
        ba = BusinessAssistantService(
            integration_environment=ENV_FIXTURE,
            conversation_gateway=_fake_gateway(),
        )
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Измени цену SKU-123 на Ozon на 49990",
        )
        self.assertNotEqual(req.intent, INTENT_CONVERSATIONAL)
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        self.assertGreater(len(plan.steps), 0)


class BusinessAssistantApiConversationalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_conv.sqlite")
        self.gateway_calls: list = []
        self.gateway = _fake_gateway(self.gateway_calls)
        self.rt = build_business_assistant_api_runtime(
            db_path=self.db,
            conversation_gateway=self.gateway,
        )
        self.svc = self.rt.service

    def tearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_api_greeting_completes_without_workflow_diagnostics(self):
        rec = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="привет как ты",
            idempotency_key="conv-greeting-1",
            conversation_id="conv-1",
        )
        self.assertEqual(rec.status, ST_COMPLETED)
        result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id)
        summary = result["summary"]
        for marker in WORKFLOW_DIAGNOSTICS:
            self.assertNotIn(marker, summary)
        self.assertEqual(summary, FAKE_REPLY)
        self.assertEqual(len(self.gateway_calls), 1)
        self.assertEqual(self.gateway_calls[0].conversation_id, "conv-1")

    def test_api_business_request_still_workflows(self):
        rec = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="Проверь товары Samsung у поставщика",
            idempotency_key="conv-business-1",
        )
        self.assertIn(rec.status, {ST_COMPLETED, "WAITING_FOR_APPROVAL", "RUNNING"})
        self.assertTrue(rec.plan_id)
        if rec.execution_id:
            result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id)
            self.assertIn("Requested:", result["summary"])

    def test_conversation_message_user_facing(self):
        self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a", title="Chat")
        self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="Кто ты?",
            idempotency_key="conv-who-1",
            conversation_id="conv-who",
        )
        msgs = self.svc.get_conversation_messages(
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-who"
        )
        assistant = [m for m in msgs if m["role"] == "assistant"]
        self.assertTrue(assistant)
        self.assertEqual(assistant[-1]["content"], FAKE_REPLY)
        for marker in WORKFLOW_DIAGNOSTICS:
            self.assertNotIn(marker, assistant[-1]["content"])

    def test_unconfigured_gateway_fails_closed_at_api(self):
        rt = build_business_assistant_api_runtime(db_path=os.path.join(self.tmp, "ba_no_gw.sqlite"))
        try:
            with self.assertRaises(BusinessAssistantApiError) as ctx:
                rt.service.submit(
                    tenant_id="tenant-a",
                    owner_id="user-a",
                    message="привет",
                    idempotency_key="conv-unavail-1",
                )
            self.assertEqual(ctx.exception.code, BAA_CONVERSATION_UNAVAILABLE)
            self.assertEqual(ctx.exception.http_status, 503)
        finally:
            rt.close()


class AsyncConversationalHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_async.sqlite")
        self.gateway_calls: list = []
        self.gateway = _fake_gateway(self.gateway_calls)
        self.rt = build_business_assistant_api_runtime(
            db_path=self.db,
            conversation_gateway=self.gateway,
        )
        self.svc = self.rt.service

    async def asyncTearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_async_api_path_uses_fake_gateway(self):
        with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool:
            rec = await self.svc.submit_async(
                tenant_id="tenant-a",
                owner_id="user-a",
                message="привет как ты",
                idempotency_key="async-greeting-1",
                conversation_id="conv-async-1",
            )
            mock_pool.assert_not_called()
        self.assertEqual(rec.status, ST_COMPLETED)
        self.assertEqual(len(self.gateway_calls), 1)
        self.assertEqual(self.gateway_calls[0].conversation_id, "conv-async-1")

    async def test_concurrent_async_conversational_requests(self):
        with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool:
            tasks = [
                self.svc.submit_async(
                    tenant_id="tenant-a",
                    owner_id="user-a",
                    message=f"привет {i}",
                    idempotency_key=f"async-conc-{i}",
                )
                for i in range(4)
            ]
            import asyncio

            results = await asyncio.gather(*tasks)
            mock_pool.assert_not_called()
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r.status == ST_COMPLETED for r in results))
        self.assertEqual(len(self.gateway_calls), 4)

    async def test_unconfigured_gateway_fails_closed_async(self):
        rt = build_business_assistant_api_runtime(db_path=os.path.join(self.tmp, "ba_async_no_gw.sqlite"))
        try:
            with self.assertRaises(BusinessAssistantApiError) as ctx:
                await rt.service.submit_async(
                    tenant_id="tenant-a",
                    owner_id="user-a",
                    message="привет",
                    idempotency_key="async-unavail-1",
                )
            self.assertEqual(ctx.exception.code, BAA_CONVERSATION_UNAVAILABLE)
            self.assertEqual(ctx.exception.http_status, 503)
        finally:
            rt.close()


class StartupWiringTests(unittest.TestCase):
    def test_wiring_missing_engine_is_observable_not_silent(self):
        ba = BusinessAssistantService()
        with self.assertLogs("business_assistant_api.runtime", level="WARNING") as logs:
            wired = wire_panda_conversation_gateway(
                ba_service=ba,
                workflow_engine=None,
                run_router=lambda: None,
                context_manager=object(),
            )
        self.assertFalse(wired)
        self.assertIsNone(ba.conversation_gateway)
        self.assertTrue(any("not wired" in m for m in logs.output))

    def test_wiring_incomplete_raises(self):
        ba = BusinessAssistantService()
        with self.assertRaises(RuntimeError):
            wire_panda_conversation_gateway(
                ba_service=ba,
                workflow_engine=object(),
                run_router=None,
                context_manager=object(),
            )
        self.assertIsNone(ba.conversation_gateway)

    def test_wiring_success_attaches_gateway(self):
        ba = BusinessAssistantService()
        engine = object()
        router_fn = lambda: None
        ctx = object()
        self.assertTrue(
            wire_panda_conversation_gateway(
                ba_service=ba,
                workflow_engine=engine,
                run_router=router_fn,
                context_manager=ctx,
            )
        )
        self.assertIsNotNone(ba.conversation_gateway)


class GovernanceBoundaryTests(unittest.TestCase):
    def test_no_integration_activation_on_conversational(self):
        activation = MagicMock(spec=IntegrationActivationService)
        gateway = _fake_gateway()
        ba = BusinessAssistantService(
            integration_activation=activation,
            integration_environment=ENV_FIXTURE,
            conversation_gateway=gateway,
        )
        req = ba.submit_request(tenant_id="tenant-a", user_id="u", text="привет")
        ba.respond_conversationally(request_id=req.request_id, tenant_id="tenant-a")
        activation.resolve_connection.assert_not_called()

    def test_fake_gateway_zero_network(self):
        """FakePandaConversationGateway is async-local only — no provider imports."""
        calls: list = []
        gateway = FakePandaConversationGateway(response="local-only", calls=calls)
        ba = BusinessAssistantService(conversation_gateway=gateway)
        req = ba.submit_request(tenant_id="t", user_id="u", text="hello")
        ex = ba.respond_conversationally(request_id=req.request_id, tenant_id="t")
        self.assertEqual(ex.summary, "local-only")
        with patch("agents.model_router.ModelRouter") as mock_router:
            mock_router.assert_not_called()

    def test_no_hardcoded_provider_in_business_assistant(self):
        import business_assistant.conversation_gateway as gw
        import business_assistant.service as svc

        for mod in (gw, svc):
            source = open(mod.__file__, encoding="utf-8").read().casefold()
            for banned in ("openai", "anthropic", "gemini", "grok", "deepseek", "mistral", "moonshot"):
                self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
