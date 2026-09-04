"""Focused local tests — canonical final_answer pipeline (no real providers)."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest

from agents.core.response_formatter import ResponseFormatter
from agents.judge import Judge
from agents.provider_result import ProviderResult
from business_assistant.conversation_gateway import (
    ConversationRequest,
    ConversationUnavailableError,
    FakePandaConversationGateway,
    WorkflowPandaConversationGateway,
    extract_assistant_text,
    select_canonical_final_answer,
)
from business_assistant_api.models import ST_COMPLETED
from business_assistant_api.runtime import build_business_assistant_api_runtime
from business_assistant_api.errors import BAA_CONVERSATION_UNAVAILABLE, BusinessAssistantApiError

JUDGE_SYNTHESIS = (
    "Синтез ответов экспертов без скрытого приоритета provider. "
    "Внешняя проверка фактов учитывается только при независимых источниках."
)
JUDGE_SUMMARY = "Финальный анализ успешно сформирован."
EXPERT_A = "Здравствуйте. Чем могу помочь по работе?"
EXPERT_B = "Могу подсказать по складу и заказам."


def _judge_payload(*, experts: dict | None = None, extra: dict | None = None) -> dict:
    payload = {
        "role": "Judge",
        "summary": JUDGE_SUMMARY,
        "best_solution": JUDGE_SYNTHESIS,
        "analysis": "",
        "confidence": 50,
    }
    if experts:
        lines = [f"{pid}: {experts[pid]}" for pid in sorted(experts)]
        payload["analysis"] = "\n".join(lines)
        payload["experts"] = dict(experts)
        payload["final_answer"] = "\n".join(experts[pid] for pid in sorted(experts))
    if extra:
        payload.update(extra)
    return payload


class CanonicalSelectorContractTests(unittest.TestCase):
    def test_explicit_synthesized_final_answer_wins(self):
        out = select_canonical_final_answer(
            {
                "final_answer": EXPERT_A,
                "best_solution": JUDGE_SYNTHESIS,
                "summary": JUDGE_SUMMARY,
                "analysis": f"openai: {EXPERT_B}",
            }
        )
        self.assertEqual(out, EXPERT_A)

    def test_experts_map_used_when_final_answer_absent(self):
        out = select_canonical_final_answer(
            {
                "summary": JUDGE_SUMMARY,
                "best_solution": JUDGE_SYNTHESIS,
                "experts": {"openai": EXPERT_A},
            }
        )
        self.assertEqual(out, EXPERT_A)

    def test_nested_orchestration_experts_survive(self):
        out = select_canonical_final_answer(
            {
                "summary": JUDGE_SUMMARY,
                "best_solution": JUDGE_SYNTHESIS,
                "result": {
                    "experts": {
                        "openai": {"text": EXPERT_A},
                        "anthropic": ProviderResult(
                            text=EXPERT_B, provider_id="anthropic", model_id="fake"
                        ),
                    }
                },
            }
        )
        self.assertEqual(out, f"{EXPERT_B}\n{EXPERT_A}")

    def test_multiple_experts_follow_sorted_provider_aggregation(self):
        out = select_canonical_final_answer(
            {
                "experts": {"openai": EXPERT_A, "anthropic": EXPERT_B},
                "best_solution": JUDGE_SYNTHESIS,
            }
        )
        self.assertEqual(out, f"{EXPERT_B}\n{EXPERT_A}")
        self.assertEqual(
            out,
            select_canonical_final_answer(
                {"experts": {"anthropic": EXPERT_B, "openai": EXPERT_A}}
            ),
        )

    def test_production_synthesis_string_never_selected(self):
        self.assertEqual(select_canonical_final_answer({"best_solution": JUDGE_SYNTHESIS}), "")
        self.assertEqual(select_canonical_final_answer({"summary": JUDGE_SYNTHESIS}), "")
        self.assertEqual(
            extract_assistant_text(
                {"summary": JUDGE_SUMMARY, "best_solution": JUDGE_SYNTHESIS, "analysis": ""}
            ),
            "",
        )

    def test_judge_fields_lose_to_expert_content(self):
        out = extract_assistant_text(_judge_payload(experts={"openai": EXPERT_A}))
        self.assertEqual(out, EXPERT_A)
        self.assertNotIn("Синтез ответов экспертов", out)
        self.assertNotIn("Финальный анализ", out)

    def test_no_legitimate_answer_is_empty(self):
        self.assertEqual(
            select_canonical_final_answer(
                {
                    "summary": JUDGE_SUMMARY,
                    "best_solution": JUDGE_SYNTHESIS,
                    "analysis": "Нет успешных ответов экспертов.",
                    "experts": {},
                }
            ),
            "",
        )

    def test_bare_provider_prefix_does_not_drop_expert_body(self):
        body = "Могу помочь с задачей по складу."
        out = select_canonical_final_answer(
            {"analysis": f"openai: {body}", "best_solution": JUDGE_SYNTHESIS}
        )
        self.assertEqual(out, body)


class JudgeFormatterPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_judge_sets_final_answer_from_experts(self):
        judge = Judge()
        decision = await judge.run(experts={"openai": EXPERT_A, "anthropic": EXPERT_B})
        self.assertEqual(decision["final_answer"], f"{EXPERT_B}\n{EXPERT_A}")
        self.assertEqual(decision["best_solution"], JUDGE_SYNTHESIS)
        self.assertEqual(decision["summary"], JUDGE_SUMMARY)
        formatted = await ResponseFormatter().format(decision)
        self.assertEqual(formatted["final_answer"], decision["final_answer"])
        self.assertEqual(extract_assistant_text(formatted), f"{EXPERT_B}\n{EXPERT_A}")

    async def test_judge_empty_experts_final_answer_empty(self):
        judge = Judge()
        decision = await judge.run(experts={})
        self.assertEqual(decision["final_answer"], "")
        self.assertEqual(extract_assistant_text(await ResponseFormatter().format(decision)), "")


class GatewayAndApiSerializerTests(unittest.TestCase):
    def test_gateway_maps_formatted_judge_output(self):
        class _Engine:
            last_workflow_id = "wf-local"

            async def execute(self, *args, **kwargs):
                return _judge_payload(experts={"openai": EXPERT_A})

        gw = WorkflowPandaConversationGateway(
            workflow_engine=_Engine(),
            run_router=object(),
            context_manager=object(),
        )

        async def _run():
            return await gw.respond(
                ConversationRequest(
                    text="привет",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    request_id="req-1",
                )
            )

        result = asyncio.run(_run())
        self.assertEqual(result.text, EXPERT_A)

    def test_orchestration_exception_is_failure_not_fake_success(self):
        class _Engine:
            async def execute(self, *args, **kwargs):
                raise RuntimeError("provider_timeout")

        gw = WorkflowPandaConversationGateway(
            workflow_engine=_Engine(),
            run_router=object(),
            context_manager=object(),
        )

        async def _run():
            await gw.respond(
                ConversationRequest(
                    text="привет",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    request_id="req-2",
                )
            )

        with self.assertRaises(ConversationUnavailableError):
            asyncio.run(_run())

    def test_api_result_preserves_final_answer(self):
        tmp = tempfile.mkdtemp()
        try:
            rt = build_business_assistant_api_runtime(
                db_path=os.path.join(tmp, "ba.sqlite"),
                conversation_gateway=FakePandaConversationGateway(response=EXPERT_A),
            )
            svc = rt.service
            conv = svc.create_conversation(tenant_id="tenant-a", owner_id="user-a")
            rec = svc.submit(
                tenant_id="tenant-a",
                owner_id="user-a",
                message="привет",
                conversation_id=conv.conversation_id,
                idempotency_key="final-pipeline-1",
            )
            self.assertEqual(rec.status, ST_COMPLETED)
            result = svc.get_result(
                tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id
            )
            self.assertEqual(result["final_answer"], EXPERT_A)
            self.assertNotEqual(result["final_answer"], JUDGE_SYNTHESIS)
            rt.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gateway_unavailable_stays_failed(self):
        tmp = tempfile.mkdtemp()
        try:
            rt = build_business_assistant_api_runtime(
                db_path=os.path.join(tmp, "ba-fail.sqlite"),
                conversation_gateway=None,
            )
            with self.assertRaises(BusinessAssistantApiError) as ctx:
                rt.service.submit(
                    tenant_id="tenant-a",
                    owner_id="user-a",
                    message="привет",
                    idempotency_key="final-pipeline-fail-1",
                )
            self.assertEqual(ctx.exception.code, BAA_CONVERSATION_UNAVAILABLE)
            self.assertNotEqual(ctx.exception.code, ST_COMPLETED)
            rt.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
