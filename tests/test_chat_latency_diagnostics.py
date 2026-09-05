"""Deterministic chat latency policy + diagnostics boundary (no live providers)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from agents.core.decision_memory import DecisionMemory
from agents.core.pipeline import Pipeline
from agents.core.response_formatter import ResponseFormatter
from agents.execution_policy import (
    POLICY_FULL,
    POLICY_LIGHTWEIGHT,
    POLICY_STANDARD,
    resolve_execution_policy,
    sanitize_latency_ms,
)
from agents.fact_validator import FactValidator
from agents.judge import Judge
from agents.peer_review import PeerReview
from agents.response_depth import DEPTH_ANALYTICAL, DEPTH_DEEP, DEPTH_DIRECT, DEPTH_NORMAL
from agents.routing_requirements import (
    CAPABILITY_SEARCH,
    FRESHNESS_CURRENT,
    RISK_HIGH,
    TaskRequirements,
    derive_task_requirements,
)
from agents.task_classifier import CATEGORY_GENERAL, CATEGORY_STRATEGY, TaskClassifier
from business_assistant.conversation_gateway import (
    ConversationRequest,
    ConversationUnavailableError,
    FakePandaConversationGateway,
    WorkflowPandaConversationGateway,
)
from business_assistant.follow_up import (
    KIND_NEW_TOPIC,
    KIND_QUESTION,
    KIND_TRANSFORM,
    HistoryTurn,
    build_follow_up_prompt,
    resolve_follow_up,
)
from business_assistant.intent import classify_intent
from business_assistant.models import (
    INTENT_CONVERSATIONAL,
    INTENT_UPDATE,
    STEP_PREPARE_WRITE,
    STEP_WRITE,
    BusinessConstraint,
)
from business_assistant.recipes import ozon_price_steps
from business_assistant_api.errors import BAA_CONVERSATION_UNAVAILABLE, BusinessAssistantApiError
from business_assistant_api.models import ST_COMPLETED, ST_FAILED
from business_assistant_api.runtime import build_business_assistant_api_runtime


RECIPE = (
    "Петуха варить на слабом огне 2,5–4 часа. Снимать пену. "
    "Процедить бульон. Картофель 10–12 минут, затем лапша."
)
OLD_COMMISSION = "Комиссия Ozon была 8% в 2020 году."
GREETING = "Здравствуйте. Чем могу помочь?"
DIAG_MARKERS = (
    "Техническая диагностика",
    "Ход выполнения",
    "Request accepted",
    "Validating request",
    "Conversational response",
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _history(*pairs: tuple[str, str]) -> tuple[HistoryTurn, ...]:
    return tuple(HistoryTurn(role, text) for role, text in pairs)


def _policy_for(text: str) -> tuple[object, str]:
    clf = TaskClassifier().classify(text)
    policy = resolve_execution_policy(
        category=clf.category,
        response_depth=clf.response_depth,
        requirements=clf.requirements,
    )
    return clf, policy


class _SearchGateway:
    def __init__(self):
        self.calls = 0
        self.task_id = ""

    def reset_budget(self):
        return None

    async def search(self, *args, **kwargs):
        self.calls += 1
        return []


async def _execute_with_spies(*, policy: str, experts: dict | None = None, category: str | None = "general"):
    expert_manager = Mock()
    expert_manager.run = AsyncMock(return_value=experts if experts is not None else {"openai": GREETING})
    expert_manager.last_errors = {}
    peer = PeerReview()
    facts = FactValidator(gateway=_SearchGateway())
    judge = Judge()
    peer_n = {"n": 0}
    fact_n = {"n": 0}
    judge_n = {"n": 0}
    orig_peer = peer.review
    orig_fact = facts.validate
    orig_judge = judge.run

    async def review(*a, **k):
        peer_n["n"] += 1
        return await orig_peer(*a, **k)

    async def validate(*a, **k):
        fact_n["n"] += 1
        return await orig_fact(*a, **k)

    async def jrun(*a, **k):
        judge_n["n"] += 1
        return await orig_judge(*a, **k)

    peer.review = review
    facts.validate = validate
    judge.run = jrun
    pipeline = Pipeline(
        expert_manager,
        peer,
        facts,
        judge,
        ResponseFormatter(),
        object(),
        DecisionMemory(),
    )
    answer = await pipeline.execute("prompt", category=category, execution_policy=policy)
    return {
        "answer": answer,
        "peer": peer_n["n"],
        "fact": fact_n["n"],
        "judge": judge_n["n"],
        "search": facts.gateway.calls,
        "pipeline": pipeline,
    }


class ExecutionPolicyTests(unittest.TestCase):
    def test_a_direct_simple_lightweight(self):
        clf, policy = _policy_for("привет")
        self.assertEqual(clf.category, CATEGORY_GENERAL)
        self.assertEqual(clf.response_depth, DEPTH_DIRECT)
        self.assertEqual(policy, POLICY_LIGHTWEIGHT)

    def test_b_normal_recipe_lightweight(self):
        clf, policy = _policy_for("суп из петуха хочу сделать")
        self.assertEqual(clf.category, CATEGORY_GENERAL)
        self.assertIn(clf.response_depth, {DEPTH_NORMAL, DEPTH_DIRECT})
        self.assertEqual(policy, POLICY_LIGHTWEIGHT)

    def test_c_contextual_transform_lightweight(self):
        current = "пометь главное"
        res = resolve_follow_up(
            current,
            _history(("user", "суп из петуха хочу сделать"), ("assistant", RECIPE), ("user", current)),
        )
        self.assertEqual(res.kind, KIND_TRANSFORM)
        prompt = build_follow_up_prompt(current, res)
        self.assertIn(RECIPE, prompt)
        self.assertNotIn("пришлите текст", prompt.casefold())
        clf, policy = _policy_for(current)
        self.assertEqual(clf.category, CATEGORY_GENERAL)
        self.assertEqual(policy, POLICY_LIGHTWEIGHT)

    def test_d_contextual_question_lightweight(self):
        current = "а сколько соли?"
        res = resolve_follow_up(
            current,
            _history(("user", "суп"), ("assistant", RECIPE), ("user", current)),
        )
        self.assertEqual(res.kind, KIND_QUESTION)
        prompt = build_follow_up_prompt(current, res)
        self.assertIn(RECIPE, prompt)
        clf, policy = _policy_for(current)
        self.assertEqual(policy, POLICY_LIGHTWEIGHT)

    def test_e_new_topic_analytical_not_lightweight(self):
        current = "Сравни Ozon и Wildberries для продажи электроники"
        res = resolve_follow_up(
            current,
            _history(("user", "суп"), ("assistant", RECIPE), ("user", current)),
        )
        self.assertEqual(res.kind, KIND_NEW_TOPIC)
        self.assertNotIn(RECIPE, build_follow_up_prompt(current, res))
        clf, policy = _policy_for(current)
        self.assertNotEqual(policy, POLICY_LIGHTWEIGHT)
        self.assertIn(clf.response_depth, {DEPTH_ANALYTICAL, DEPTH_DEEP})

    def test_f_current_freshness_overrides_lightweight(self):
        current = "а какая комиссия сейчас?"
        res = resolve_follow_up(
            current,
            _history(("user", "ozon"), ("assistant", OLD_COMMISSION), ("user", current)),
        )
        prompt = build_follow_up_prompt(current, res)
        clf = TaskClassifier().classify(prompt)
        req = derive_task_requirements(category=clf.category, text=prompt)
        self.assertEqual(req.freshness, FRESHNESS_CURRENT)
        policy = resolve_execution_policy(
            category=clf.category,
            response_depth=clf.response_depth,
            requirements=req,
        )
        self.assertEqual(policy, POLICY_FULL)
        self.assertNotEqual(policy, POLICY_LIGHTWEIGHT)

    def test_g_search_capability_not_lightweight(self):
        policy = resolve_execution_policy(
            category=CATEGORY_GENERAL,
            response_depth=DEPTH_DIRECT,
            requirements=TaskRequirements(
                freshness=FRESHNESS_CURRENT,
                required_capabilities=(CAPABILITY_SEARCH,),
            ),
        )
        self.assertEqual(policy, POLICY_FULL)

    def test_h_write_stays_business_intent(self):
        text = "Измени цену SKU-123 на Ozon на 49990"
        self.assertNotEqual(classify_intent(text), INTENT_CONVERSATIONAL)
        self.assertEqual(classify_intent(text), INTENT_UPDATE)

    def test_h_hitl_write_recipes_still_require_approval(self):
        steps = ozon_price_steps(constraints=BusinessConstraint(read_only=False))
        write_hitl = [
            s
            for s in steps
            if s.step_class in {STEP_WRITE, STEP_PREPARE_WRITE} and s.requires_approval
        ]
        self.assertTrue(write_hitl)

    def test_i_analytical_not_lightweight(self):
        clf, policy = _policy_for("Сравни Ozon и Wildberries для продажи электроники")
        self.assertIn(clf.response_depth, {DEPTH_ANALYTICAL, DEPTH_DEEP})
        self.assertIn(policy, {POLICY_STANDARD, POLICY_FULL})
        self.assertNotEqual(policy, POLICY_LIGHTWEIGHT)

    def test_j_deep_full(self):
        clf, policy = _policy_for("Разработай стратегию продаж нового товара.")
        self.assertEqual(clf.category, CATEGORY_STRATEGY)
        self.assertEqual(clf.response_depth, DEPTH_DEEP)
        self.assertEqual(policy, POLICY_FULL)

    def test_high_risk_not_lightweight(self):
        policy = resolve_execution_policy(
            category=CATEGORY_GENERAL,
            response_depth=DEPTH_DIRECT,
            requirements=TaskRequirements(risk=RISK_HIGH),
        )
        self.assertEqual(policy, POLICY_FULL)


class PipelineStageSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_lightweight_skips_peer_fact_judge(self):
        out = await _execute_with_spies(policy=POLICY_LIGHTWEIGHT)
        self.assertEqual(out["peer"], 0)
        self.assertEqual(out["fact"], 0)
        self.assertEqual(out["judge"], 0)
        self.assertEqual(out["search"], 0)
        self.assertEqual(out["answer"]["final_answer"], GREETING)
        self.assertNotIn("prompt", json.dumps(out["pipeline"].last_latency_ms))
        self.assertIn("provider_ms", out["pipeline"].last_latency_ms)
        self.assertIn("request_total_ms", out["pipeline"].last_latency_ms)

    async def test_standard_keeps_peer_judge_skips_fact(self):
        out = await _execute_with_spies(policy=POLICY_STANDARD)
        self.assertEqual(out["peer"], 1)
        self.assertEqual(out["judge"], 1)
        self.assertEqual(out["fact"], 0)
        self.assertEqual(out["search"], 0)

    async def test_full_keeps_fact_and_judge(self):
        long_claim = "Ozon комиссия составляет 15 процентов согласно внутреннему расчету рынка 2024."
        out = await _execute_with_spies(
            policy=POLICY_FULL,
            experts={"openai": long_claim},
            category="general",
        )
        self.assertEqual(out["peer"], 1)
        self.assertEqual(out["judge"], 1)
        self.assertEqual(out["fact"], 1)

    async def test_q_lightweight_empty_experts_is_failure(self):
        with self.assertRaises(RuntimeError):
            await _execute_with_spies(policy=POLICY_LIGHTWEIGHT, experts={})


class LatencySanitizeTests(unittest.TestCase):
    def test_p_timing_drops_payloads(self):
        cleaned = sanitize_latency_ms(
            {
                "provider_ms": 12,
                "prompt": "секретный промпт",
                "final_answer": GREETING,
                "api_key": "sk-secret",
                "judge_ms": "3",
                "unknown_ms": 9,
            }
        )
        self.assertEqual(cleaned["provider_ms"], 12)
        self.assertEqual(cleaned["judge_ms"], 3)
        blob = json.dumps(cleaned)
        self.assertNotIn("секретный", blob)
        self.assertNotIn(GREETING, blob)
        self.assertNotIn("sk-secret", blob)
        self.assertNotIn("prompt", blob)


class DiagnosticsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "ba.sqlite"),
            conversation_gateway=FakePandaConversationGateway(response=GREETING),
        )
        self.addCleanup(self.rt.close)
        self.svc = self.rt.service

    def test_n_o_ordinary_result_hides_diagnostics_and_duplicates(self):
        conv = self.svc.create_conversation(tenant_id="tenant-a", owner_id="user-a")
        rec = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="привет",
            conversation_id=conv.conversation_id,
            idempotency_key="lat-diag-1",
        )
        self.assertEqual(rec.status, ST_COMPLETED)
        result = self.svc.get_result(
            tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id
        )
        self.assertEqual(result["final_answer"], GREETING)
        payload = json.dumps(result, ensure_ascii=False)
        for marker in DIAG_MARKERS:
            self.assertNotIn(marker, result["final_answer"])
            self.assertNotIn(marker, payload)
        self.assertNotIn("latency_ms", result)
        self.assertNotIn("execution_policy", result)
        self.assertEqual(result["final_answer"], result.get("summary") or result["final_answer"])
        events = self.svc.list_events(
            tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id
        )
        completed = [e for e in events if e.event_type == "REQUEST_COMPLETED"]
        self.assertTrue(completed)
        self.assertNotEqual(completed[0].message, GREETING)
        msgs = self.svc.store.list_messages(
            tenant_id="tenant-a", conversation_id=conv.conversation_id
        )
        assistant = [m for m in msgs if m.role == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0].content, GREETING)

    def test_q_provider_failure_no_fake_answer(self):
        class Boom:
            async def respond(self, request):
                raise ConversationUnavailableError("provider_timeout")

        rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "fail.sqlite"),
            conversation_gateway=Boom(),
        )
        self.addCleanup(rt.close)
        conv = rt.service.create_conversation(tenant_id="tenant-a", owner_id="user-a")
        with self.assertRaises(BusinessAssistantApiError) as ctx:
            rt.service.submit(
                tenant_id="tenant-a",
                owner_id="user-a",
                message="привет",
                conversation_id=conv.conversation_id,
                idempotency_key="lat-fail-1",
            )
        self.assertEqual(ctx.exception.code, BAA_CONVERSATION_UNAVAILABLE)
        self.assertNotIn("Traceback", str(ctx.exception))
        rec = rt.service.store.get_request_by_idempotency(
            tenant_id="tenant-a", idempotency_key="lat-fail-1"
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec.status, ST_FAILED)
        msgs = rt.service.store.list_messages(
            tenant_id="tenant-a", conversation_id=conv.conversation_id
        )
        self.assertFalse(any(m.role == "assistant" for m in msgs))
        self.assertTrue(any(m.role == "user" for m in msgs))


class GatewayFollowUpKindTests(unittest.IsolatedAsyncioTestCase):
    async def test_follow_up_kind_forwarded_without_second_pipeline(self):
        captured = {}

        async def run_router(**kwargs):
            captured.update(kwargs)
            return {"final_answer": "Главное: варить долго."}

        class Engine:
            last_workflow_id = "wf-1"

            async def execute(self, prompt, mode, role, **kwargs):
                return await kwargs["run_router"](prompt=prompt, mode=mode, role=role)

        gw = WorkflowPandaConversationGateway(
            workflow_engine=Engine(),
            run_router=run_router,
            context_manager=object(),
        )
        current = "пометь главное"
        result = await gw.respond(
            ConversationRequest(
                text=current,
                tenant_id="t1",
                user_id="u1",
                request_id="r1",
                history=_history(
                    ("user", "суп из петуха хочу сделать"),
                    ("assistant", RECIPE),
                    ("user", current),
                ),
            )
        )
        self.assertEqual(captured.get("follow_up_kind"), KIND_TRANSFORM)
        self.assertEqual(captured.get("classification_text"), current)
        self.assertEqual(result.text, "Главное: варить долго.")
        self.assertNotIn("latency_ms", result.metadata)


class FrontendDiagnosticsContractTests(unittest.TestCase):
    def test_chat_never_unlocks_diagnostics(self):
        app = _read("static/panda/js/app.js")
        self.assertIn("function canShowDiagnostics()", app)
        self.assertIn("return false;", app)
        self.assertNotIn("?debug=true", app)
        html = _read("static/panda/index.html")
        self.assertIn("diagnostics-panel", html)
        admin = _read("static/admin/index.html")
        self.assertIn("Техническая диагностика", admin)


class StreamingInventoryTests(unittest.TestCase):
    def test_no_chat_sse_subsystem(self):
        app = _read("static/panda/js/app.js")
        self.assertNotIn("EventSource", app)
        self.assertNotIn("text/event-stream", app)


if __name__ == "__main__":
    unittest.main()
