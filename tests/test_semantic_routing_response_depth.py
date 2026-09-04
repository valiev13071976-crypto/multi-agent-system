"""Deterministic semantic routing + response depth (no real providers)."""

from __future__ import annotations

import unittest

from agents.response_depth import (
    DEPTH_ANALYTICAL,
    DEPTH_DEEP,
    DEPTH_DIRECT,
    DEPTH_NORMAL,
    ORCHESTRATION_FULL_PIPELINE,
    STRATEGIST_FRAMEWORK_MARKERS,
    classify_response_depth,
    contains_strategist_framework,
    orchestration_policy_for,
)
from agents.role_registry import compose_prompt, instruction_for_role
from agents.routing_requirements import FRESHNESS_CURRENT, derive_task_requirements
from agents.task_classifier import (
    CATEGORY_GENERAL,
    CATEGORY_STRATEGY,
    ROLE_CRITIC,
    ROLE_GENERALIST,
    ROLE_RESEARCHER,
    ROLE_STRATEGIST,
    ROLE_TECHNICAL,
    ROLE_TREND_AGENT,
    TaskClassifier,
)
from business_assistant.intent import classify_intent, requires_business_integration
from business_assistant.models import INTENT_CONVERSATIONAL
from business_assistant.conversation_gateway import select_canonical_final_answer


E2 = (
    "Проанализируй экономику выхода нашего магазина на Ozon, "
    "учти комиссию, логистику, рекламу, НДС и предложи стратегию запуска."
)


class SemanticRoutingResponseDepthTests(unittest.TestCase):
    def setUp(self):
        self.classifier = TaskClassifier()

    def _route(self, text: str):
        result = self.classifier.classify(text)
        composed = compose_prompt(
            result.role_id,
            text,
            response_depth=result.response_depth,
        )
        return result, composed

    def _assert_no_strategist_framework(self, composed: str):
        for marker in STRATEGIST_FRAMEWORK_MARKERS:
            self.assertNotIn(marker, composed)

    def test_a1_greeting_direct_generalist(self):
        result, composed = self._route("привет")
        self.assertEqual(result.category, CATEGORY_GENERAL)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(result.response_depth, DEPTH_DIRECT)
        self.assertEqual(result.requirements.required_capabilities, ())
        self._assert_no_strategist_framework(composed)
        self.assertIn("DIRECT", composed)

    def test_a2_zdravstvuyte(self):
        self.assertEqual(self.classifier.classify("здравствуйте").response_depth, DEPTH_DIRECT)

    def test_a3_thanks(self):
        self.assertEqual(self.classifier.classify("спасибо").response_depth, DEPTH_DIRECT)

    def test_a4_goodbye(self):
        self.assertEqual(self.classifier.classify("пока").response_depth, DEPTH_DIRECT)

    def test_a5_how_are_you(self):
        result, composed = self._route("как дела?")
        self.assertEqual(result.response_depth, DEPTH_DIRECT)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self._assert_no_strategist_framework(composed)

    def test_b1_nds_normal(self):
        result, composed = self._route("Что такое НДС?")
        self.assertEqual(result.category, CATEGORY_GENERAL)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(result.response_depth, DEPTH_NORMAL)
        self._assert_no_strategist_framework(composed)

    def test_b2_leap_year(self):
        self.assertEqual(
            self.classifier.classify("Сколько дней в високосном году?").response_depth,
            DEPTH_NORMAL,
        )

    def test_b3_api_explain(self):
        result, composed = self._route("Объясни простыми словами, что такое API.")
        self.assertEqual(result.response_depth, DEPTH_NORMAL)
        self._assert_no_strategist_framework(composed)

    def test_c1_margin(self):
        result, composed = self._route("Как посчитать маржу?")
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(result.response_depth, DEPTH_NORMAL)
        self._assert_no_strategist_framework(composed)

    def test_c2_gross_profit(self):
        self.assertEqual(
            self.classifier.classify("Что такое валовая прибыль?").response_depth,
            DEPTH_NORMAL,
        )

    def test_c3_revenue_vs_profit(self):
        result, composed = self._route("Чем выручка отличается от прибыли?")
        self.assertEqual(result.response_depth, DEPTH_NORMAL)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self._assert_no_strategist_framework(composed)

    def test_d1_advisory_analytical(self):
        text = "Помоги выбрать между Ozon и Wildberries для нового товара."
        result, composed = self._route(text)
        self.assertEqual(result.category, CATEGORY_GENERAL)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(result.response_depth, DEPTH_ANALYTICAL)
        self._assert_no_strategist_framework(composed)
        self.assertIn("ANALYTICAL", composed)

    def test_e1_sales_strategy_deep_strategist(self):
        text = "Разработай стратегию продаж нового товара."
        result, composed = self._route(text)
        self.assertEqual(result.category, CATEGORY_STRATEGY)
        self.assertEqual(result.role_id, ROLE_STRATEGIST)
        self.assertEqual(result.response_depth, DEPTH_DEEP)
        self.assertTrue(contains_strategist_framework(composed))

    def test_e2_complex_ozon_launch_deep(self):
        result, composed = self._route(E2)
        self.assertEqual(result.category, CATEGORY_STRATEGY)
        self.assertEqual(result.role_id, ROLE_STRATEGIST)
        self.assertEqual(result.response_depth, DEPTH_DEEP)
        self.assertTrue(contains_strategist_framework(composed))

    def test_f1_research_integration_not_disabled_by_depth(self):
        text = "Проверь актуальные комиссии Ozon и Wildberries и сравни их."
        self.assertTrue(requires_business_integration(text))
        self.assertNotEqual(classify_intent(text), INTENT_CONVERSATIONAL)
        result = self.classifier.classify(text)
        self.assertEqual(
            derive_task_requirements(category=result.category, text=text).freshness,
            FRESHNESS_CURRENT,
        )
        self.assertNotEqual(result.response_depth, DEPTH_DIRECT)

    def test_g1_excel_business_route(self):
        text = "Проанализируй этот Excel."
        self.assertTrue(requires_business_integration(text))

    def test_g2_cards_not_strategist_report(self):
        text = "Подготовь карточки для выбранных товаров."
        result, composed = self._route(text)
        self.assertNotEqual(result.role_id, ROLE_STRATEGIST)
        self._assert_no_strategist_framework(composed)

    def test_m1_deep_then_thanks_is_direct(self):
        first = self.classifier.classify(E2)
        second = self.classifier.classify("спасибо")
        self.assertEqual(first.response_depth, DEPTH_DEEP)
        self.assertEqual(second.response_depth, DEPTH_DIRECT)
        self.assertEqual(second.role_id, ROLE_GENERALIST)

    def test_m2_analytical_then_short_is_reduced(self):
        first = self.classifier.classify(
            "Помоги выбрать между Ozon и Wildberries для нового товара."
        )
        second = self.classifier.classify("а теперь коротко")
        self.assertEqual(first.response_depth, DEPTH_ANALYTICAL)
        self.assertEqual(second.response_depth, DEPTH_DIRECT)

    def test_u1_ambiguous_is_normal(self):
        result = self.classifier.classify("что думаешь об этом")
        self.assertEqual(result.response_depth, DEPTH_NORMAL)
        self.assertEqual(result.role_id, ROLE_GENERALIST)

    def test_uc1_short_margin(self):
        text = "Ответь коротко: что такое маржа?"
        result, composed = self._route(text)
        self.assertEqual(result.response_depth, DEPTH_DIRECT)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self._assert_no_strategist_framework(composed)

    def test_uc2_detailed_revenue_not_strategist(self):
        text = "Разбери подробно, чем выручка отличается от прибыли."
        result, composed = self._route(text)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(result.response_depth, DEPTH_ANALYTICAL)
        self._assert_no_strategist_framework(composed)

    def test_uc3_short_current_commission_keeps_freshness(self):
        text = "Ответь коротко, какая сейчас комиссия Ozon?"
        result, composed = self._route(text)
        self.assertEqual(result.response_depth, DEPTH_DIRECT)
        self._assert_no_strategist_framework(composed)
        req = derive_task_requirements(category=result.category, text=text)
        self.assertEqual(req.freshness, FRESHNESS_CURRENT)
        self.assertEqual(result.requirements.freshness, FRESHNESS_CURRENT)

    def test_r1_explicit_strategist_keeps_role(self):
        text = "Разработай стратегию продаж нового товара."
        composed = compose_prompt(ROLE_STRATEGIST, text, response_depth=DEPTH_DEEP)
        self.assertIn("РОЛЬ: Стратег.", composed)
        self.assertTrue(contains_strategist_framework(composed))

    def test_r2_explicit_researcher(self):
        composed = compose_prompt(
            ROLE_RESEARCHER,
            "найди источники и проверь факты",
            response_depth=DEPTH_ANALYTICAL,
        )
        self.assertIn("РОЛЬ: Исследователь", composed)

    def test_r3_explicit_technical(self):
        composed = instruction_for_role(ROLE_TECHNICAL, DEPTH_ANALYTICAL)
        self.assertIn("РОЛЬ: Технический эксперт.", composed)

    def test_r4_explicit_critic(self):
        composed = instruction_for_role(ROLE_CRITIC, DEPTH_ANALYTICAL)
        self.assertIn("РОЛЬ: Критик.", composed)

    def test_r5_explicit_trend(self):
        composed = instruction_for_role(ROLE_TREND_AGENT, DEPTH_ANALYTICAL)
        self.assertIn("РОЛЬ: Аналитик трендов.", composed)

    def test_explicit_strategist_greeting_skips_framework(self):
        composed = compose_prompt(ROLE_STRATEGIST, "привет", response_depth=DEPTH_DIRECT)
        self.assertIn("РОЛЬ: Стратег.", composed)
        self._assert_no_strategist_framework(composed)

    def test_ba_greeting_stays_conversational_not_direct_intent(self):
        self.assertEqual(classify_intent("привет"), INTENT_CONVERSATIONAL)
        self.assertEqual(classify_intent(E2), INTENT_CONVERSATIONAL)

    def test_orchestration_policy_recorded_not_collapsed(self):
        self.assertEqual(orchestration_policy_for(DEPTH_DIRECT), ORCHESTRATION_FULL_PIPELINE)
        self.assertEqual(orchestration_policy_for(DEPTH_DEEP), ORCHESTRATION_FULL_PIPELINE)

    def test_depth_uncertain_without_category_is_normal(self):
        self.assertEqual(classify_response_depth("xyzzy"), DEPTH_NORMAL)

    def test_fa1_legitimate_expert_final_answer(self):
        text = select_canonical_final_answer(
            {
                "final_answer": "Полезный ответ эксперта.",
                "best_solution": (
                    "Синтез ответов экспертов без скрытого приоритета provider."
                ),
                "summary": "Финальный анализ успешно сформирован.",
            }
        )
        self.assertEqual(text, "Полезный ответ эксперта.")

    def test_fa2_governance_not_final_answer(self):
        payload = {
            "role": "Judge",
            "summary": "Финальный анализ успешно сформирован.",
            "best_solution": (
                "Синтез ответов экспертов без скрытого приоритета provider. "
                "Внешняя проверка фактов учитывается только при независимых источниках."
            ),
            "analysis": "",
            "final_answer": "",
            "experts": {},
        }
        text = select_canonical_final_answer(payload)
        self.assertFalse(str(text or "").strip())


if __name__ == "__main__":
    unittest.main()
