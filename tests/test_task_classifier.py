import os
import unittest
from unittest.mock import patch

from agents.task_classifier import (
    CATEGORY_CRITIQUE,
    CATEGORY_GENERAL,
    CATEGORY_RESEARCH,
    CATEGORY_STRATEGY,
    CATEGORY_TECHNICAL,
    CATEGORY_TREND_ANALYSIS,
    ROLE_CRITIC,
    ROLE_GENERALIST,
    ROLE_RESEARCHER,
    ROLE_STRATEGIST,
    ROLE_TECHNICAL,
    ROLE_TREND_AGENT,
    TaskClassifier,
    TaskClassification,
)


TRACEBACK_SAMPLE = """
Traceback (most recent call last):
  File "app.py", line 10, in <module>
    main()
TypeError: 'NoneType' object is not iterable
"""

CODE_SAMPLE = """
```python
def run():
    raise RuntimeError("broken")
```
"""


class TaskClassifierTests(unittest.TestCase):

    def setUp(self):
        self.classifier = TaskClassifier()

    def _assert_result(self, text, category, role_id):
        result = self.classifier.classify(text)
        self.assertIsInstance(result, TaskClassification)
        self.assertEqual(result.category, category)
        self.assertEqual(result.role_id, role_id)
        return result

    def test_sales_strategy_maps_to_strategist(self):
        self._assert_result(
            "придумай стратегию продаж",
            CATEGORY_STRATEGY,
            ROLE_STRATEGIST,
        )

    def test_find_errors_maps_to_critic(self):
        self._assert_result(
            "найди ошибки и слабые места в этом решении",
            CATEGORY_CRITIQUE,
            ROLE_CRITIC,
        )

    def test_market_trends_maps_to_trend_agent(self):
        self._assert_result(
            "какие сейчас тренды и динамика рынка",
            CATEGORY_TREND_ANALYSIS,
            ROLE_TREND_AGENT,
        )

    def test_sources_and_facts_map_to_researcher(self):
        self._assert_result(
            "найди источники и проверь факты",
            CATEGORY_RESEARCH,
            ROLE_RESEARCHER,
        )

    def test_traceback_maps_to_technical(self):
        self._assert_result(
            TRACEBACK_SAMPLE,
            CATEGORY_TECHNICAL,
            ROLE_TECHNICAL,
        )

    def test_python_code_maps_to_technical(self):
        self._assert_result(
            CODE_SAMPLE,
            CATEGORY_TECHNICAL,
            ROLE_TECHNICAL,
        )

    def test_analyze_market_is_general(self):
        self._assert_result(
            "проанализируй рынок",
            CATEGORY_GENERAL,
            ROLE_GENERALIST,
        )

    def test_find_supplier_is_general(self):
        self._assert_result(
            "найди поставщика",
            CATEGORY_GENERAL,
            ROLE_GENERALIST,
        )

    def test_seo_audit_is_general(self):
        self._assert_result(
            "сделай SEO аудит",
            CATEGORY_GENERAL,
            ROLE_GENERALIST,
        )

    def test_excel_prices_are_general_not_technical(self):
        self._assert_result(
            "сравни цены в Excel",
            CATEGORY_GENERAL,
            ROLE_GENERALIST,
        )

    def test_same_input_is_deterministic(self):
        text = "найди источники и проверь факты"
        first = self.classifier.classify(text)
        second = self.classifier.classify(text)
        self.assertEqual(first, second)

    def test_classification_does_not_depend_on_provider_env(self):
        text = "придумай стратегию продаж"
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "OPENAI_MODEL": "",
                "ANTHROPIC_API_KEY": "fake",
                "ANTHROPIC_MODEL": "fake-model",
            },
            clear=False,
        ):
            result = self.classifier.classify(text)
        self.assertEqual(result.category, CATEGORY_STRATEGY)
        self.assertEqual(result.role_id, ROLE_STRATEGIST)


if __name__ == "__main__":
    unittest.main()
