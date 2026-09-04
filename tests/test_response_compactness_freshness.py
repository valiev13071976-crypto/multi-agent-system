"""Compact presentation + freshness governance (no real providers)."""

from __future__ import annotations

import unittest

from agents.answer_presentation import (
    PRESENTATION_BOUNDED_ANALYTICAL,
    PRESENTATION_COMPACT_DIRECT,
    PRESENTATION_COMPACT_NORMAL,
    PRESENTATION_DETAILED_DEEP,
    presentation_policy_for,
)
from agents.response_depth import (
    DEPTH_ANALYTICAL,
    DEPTH_DEEP,
    DEPTH_DIRECT,
    DEPTH_NORMAL,
    STRATEGIST_FRAMEWORK_MARKERS,
    classify_response_depth,
    contains_strategist_framework,
)
from agents.role_registry import compose_prompt
from agents.routing_requirements import (
    CAPABILITY_SEARCH,
    FRESHNESS_CURRENT,
    FRESHNESS_HISTORICAL,
    FRESHNESS_STATIC,
    derive_task_requirements,
)
from agents.task_classifier import (
    CATEGORY_GENERAL,
    CATEGORY_STRATEGY,
    ROLE_GENERALIST,
    ROLE_STRATEGIST,
    TaskClassifier,
)
from business_assistant.intent import requires_business_integration
from business_assistant.conversation_gateway import select_canonical_final_answer


RECIPE = "суп из петуха хочу сделать"
API_Q = "Объясни простыми словами, что такое API"
COMPARE = "Сравни Ozon и Wildberries для продажи электроники"
DEEP_STRATEGY = (
    "Разработай подробную стратегию выхода на Ozon и Wildberries "
    "для магазина электроники с учетом маржи, логистики, рекламы, "
    "возвратов, налогов и рисков"
)
CURRENT_COMMISSION = "Коротко: какие сейчас комиссии Ozon для электроники?"
HISTORICAL_COMMISSION = "Какие комиссии Ozon были в 2024 году?"
STALE_AS_CURRENT = "Какие сейчас комиссии Ozon? Используй ориентиры 2024 как текущие."


class CompactnessAndFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.classifier = TaskClassifier()

    def _route(self, text: str):
        result = self.classifier.classify(text)
        composed = compose_prompt(
            result.role_id,
            text,
            response_depth=result.response_depth,
            requirements=result.requirements,
        )
        return result, composed

    def test_1_direct_greeting_compact_policy(self):
        result, composed = self._route("привет")
        self.assertEqual(result.response_depth, DEPTH_DIRECT)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(presentation_policy_for(result.response_depth), PRESENTATION_COMPACT_DIRECT)
        self.assertIn("compact_direct", composed)
        self.assertNotIn("[Факт]", composed)
        for marker in STRATEGIST_FRAMEWORK_MARKERS:
            self.assertNotIn(marker, composed)

    def test_2_normal_api_explanation_compact(self):
        result, composed = self._route(API_Q)
        self.assertEqual(result.response_depth, DEPTH_NORMAL)
        self.assertEqual(presentation_policy_for(result.response_depth), PRESENTATION_COMPACT_NORMAL)
        self.assertIn("compact_normal", composed)
        self.assertIn("первом абзаце", composed)

    def test_3_recipe_normal_not_strategist(self):
        result, composed = self._route(RECIPE)
        self.assertEqual(result.category, CATEGORY_GENERAL)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(result.response_depth, DEPTH_NORMAL)
        self.assertFalse(contains_strategist_framework(composed))
        self.assertEqual(result.requirements.freshness, FRESHNESS_STATIC)

    def test_4_analytical_comparison_bounded(self):
        result, composed = self._route(COMPARE)
        self.assertEqual(result.response_depth, DEPTH_ANALYTICAL)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(
            presentation_policy_for(result.response_depth),
            PRESENTATION_BOUNDED_ANALYTICAL,
        )
        self.assertIn("bounded_analytical", composed)
        self.assertFalse(contains_strategist_framework(composed))
        self.assertEqual(result.requirements.freshness, FRESHNESS_CURRENT)
        self.assertIn("CURRENT", composed)

    def test_5_deep_strategy_keeps_framework(self):
        result, composed = self._route(DEEP_STRATEGY)
        self.assertEqual(result.category, CATEGORY_STRATEGY)
        self.assertEqual(result.role_id, ROLE_STRATEGIST)
        self.assertEqual(result.response_depth, DEPTH_DEEP)
        self.assertEqual(presentation_policy_for(result.response_depth), PRESENTATION_DETAILED_DEEP)
        self.assertTrue(contains_strategist_framework(composed))
        self.assertEqual(result.requirements.freshness, FRESHNESS_CURRENT)

    def test_6_brevity_two_words_reduces_depth(self):
        text = "в двух словах, что такое API"
        result, composed = self._route(text)
        self.assertEqual(result.response_depth, DEPTH_DIRECT)
        self.assertIn("compact_direct", composed)

    def test_7_detail_override_increases(self):
        text = "полный разбор: чем выручка отличается от прибыли"
        result = self.classifier.classify(text)
        self.assertEqual(result.role_id, ROLE_GENERALIST)
        self.assertEqual(result.response_depth, DEPTH_ANALYTICAL)

    def test_8_brevity_does_not_clear_freshness(self):
        result, composed = self._route(CURRENT_COMMISSION)
        self.assertEqual(result.response_depth, DEPTH_DIRECT)
        self.assertEqual(result.requirements.freshness, FRESHNESS_CURRENT)
        self.assertIn("не выдумывай проценты", composed)

    def test_9_brevity_does_not_clear_integration(self):
        text = "Коротко покажи мою текущую комиссию Ozon"
        self.assertTrue(requires_business_integration(text))
        result = self.classifier.classify(text)
        self.assertEqual(result.requirements.freshness, FRESHNESS_CURRENT)

    def test_10_current_commission_marked_current(self):
        req = derive_task_requirements(category="general", text=CURRENT_COMMISSION)
        self.assertEqual(req.freshness, FRESHNESS_CURRENT)
        self.assertNotIn(CAPABILITY_SEARCH, req.required_capabilities)

    def test_11_stale_year_not_verified_current(self):
        req = derive_task_requirements(category="general", text=STALE_AS_CURRENT)
        self.assertEqual(req.freshness, FRESHNESS_CURRENT)
        composed = compose_prompt(
            ROLE_GENERALIST,
            STALE_AS_CURRENT,
            response_depth=DEPTH_DIRECT,
            requirements=req,
        )
        self.assertIn("Не подставляй устаревшие ориентиры", composed)
        self.assertIn("как актуальный факт", composed)

    def test_12_historical_2024_is_historical(self):
        req = derive_task_requirements(category="general", text=HISTORICAL_COMMISSION)
        self.assertEqual(req.freshness, FRESHNESS_HISTORICAL)
        composed = compose_prompt(
            ROLE_GENERALIST,
            HISTORICAL_COMMISSION,
            response_depth=DEPTH_NORMAL,
            requirements=req,
        )
        self.assertIn("HISTORICAL", composed)
        self.assertNotIn("Свежесть данных: CURRENT.", composed)

    def test_13_general_maps_generalist(self):
        self.assertEqual(self.classifier.classify(RECIPE).role_id, ROLE_GENERALIST)

    def test_14_strategy_maps_strategist(self):
        self.assertEqual(self.classifier.classify(DEEP_STRATEGY).role_id, ROLE_STRATEGIST)

    def test_15_final_answer_selector_unchanged(self):
        out = select_canonical_final_answer(
            {
                "final_answer": "Короткий ответ.",
                "best_solution": "Синтез ответов экспертов без скрытого приоритета provider.",
                "summary": "Финальный анализ успешно сформирован.",
            }
        )
        self.assertEqual(out, "Короткий ответ.")

    def test_16_no_epistemic_label_requirement_direct_normal(self):
        _, direct = self._route("привет")
        _, normal = self._route(API_Q)
        blob = direct + normal
        for composed in (direct, normal):
            self.assertNotIn("[Факт]", composed)
            self.assertNotIn("[Мнение]", composed)
            self.assertNotIn("[Предположение]", composed)
        self.assertIn("механическ", blob.lower())

    def test_17_analytical_generalist_no_seven_section(self):
        _, composed = self._route(COMPARE)
        self.assertFalse(contains_strategist_framework(composed))

    def test_20_unknown_presentation_is_normal_not_deep(self):
        self.assertEqual(presentation_policy_for(None), PRESENTATION_COMPACT_NORMAL)
        self.assertEqual(presentation_policy_for("nope"), PRESENTATION_COMPACT_NORMAL)
        self.assertEqual(classify_response_depth("xyzzy"), DEPTH_NORMAL)

    def test_no_hard_truncation_helpers(self):
        import agents.role_registry as rr
        import inspect

        src = inspect.getsource(rr.compose_prompt)
        self.assertNotIn("[:1000]", src)
        self.assertNotIn("final_answer[:", src)


if __name__ == "__main__":
    unittest.main()
