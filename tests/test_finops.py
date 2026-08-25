from datetime import datetime, timezone
from decimal import Decimal
import unittest

from fastapi.testclient import TestClient

from finops.models import BudgetLimits, PriceQuote, UsageRecord
from finops.service import FinOpsService, estimate_cost
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


QUOTE = PriceQuote(
    provider_id="openai",
    model_id="gpt-test",
    input_price_per_million=Decimal("3"),
    output_price_per_million=Decimal("6"),
    currency="USD",
    enabled=True,
)


class FinOpsTests(unittest.TestCase):

    def test_a_known_usage_and_price_is_deterministic(self):
        cost = estimate_cost(QUOTE, 1_000_000, 500_000)
        self.assertEqual(cost, Decimal("6"))
        again = estimate_cost(QUOTE, 1_000_000, 500_000)
        self.assertEqual(cost, again)

    def test_b_unknown_tokens_mean_unknown_cost(self):
        self.assertIsNone(estimate_cost(QUOTE, None, 10))
        self.assertIsNone(estimate_cost(QUOTE, 10, None))

    def test_c_unknown_pricing_means_unknown_cost(self):
        service = FinOpsService(prices={})
        self.assertIsNone(service.estimate("openai", "gpt-test", 100, 20))

    def test_exact_model_quote_is_used(self):
        quote_a = PriceQuote(
            provider_id="openai",
            model_id="model-a",
            input_price_per_million=Decimal("3"),
            output_price_per_million=Decimal("6"),
            currency="USD",
            enabled=True,
        )
        service = FinOpsService(prices={("openai", "model-a"): quote_a})
        self.assertIs(service.quote("openai", "model-a"), quote_a)
        self.assertEqual(service.estimate("openai", "model-a", 1_000_000, 500_000), Decimal("6"))

    def test_wrong_model_quote_is_not_used(self):
        quote_a = PriceQuote(
            provider_id="openai",
            model_id="model-a",
            input_price_per_million=Decimal("3"),
            output_price_per_million=Decimal("6"),
            currency="USD",
            enabled=True,
        )
        service = FinOpsService(prices={("openai", "model-a"): quote_a})
        self.assertIsNone(service.quote("openai", "model-b"))
        self.assertIsNone(service.estimate("openai", "model-b", 1_000_000, 500_000))

    def test_cross_provider_quote_is_not_used(self):
        quote_anthropic = PriceQuote(
            provider_id="anthropic",
            model_id="model-a",
            input_price_per_million=Decimal("3"),
            output_price_per_million=Decimal("6"),
            currency="USD",
            enabled=True,
        )
        service = FinOpsService(prices={("anthropic", "model-a"): quote_anthropic})
        self.assertIsNone(service.quote("openai", "model-a"))
        self.assertIsNone(service.estimate("openai", "model-a", 1_000_000, 500_000))

    def test_d_per_task_limit_blocks_only_when_calculable(self):
        service = FinOpsService(
            prices={("openai", "gpt-test"): QUOTE},
            limits=BudgetLimits(
                per_task=Decimal("1"),
                per_day=None,
                per_month=None,
                unknown_cost_policy="allow",
            ),
        )
        blocked = service.check_budget(Decimal("2"))
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "per_task_limit")
        unknown = service.check_budget(None)
        self.assertTrue(unknown.allowed)
        self.assertEqual(unknown.reason, "unknown_cost_allowed")

    def test_e_daily_and_monthly_totals_are_deterministic(self):
        service = FinOpsService(
            prices={("openai", "gpt-test"): QUOTE},
       !    limits=BudgetLimits(
                per_task=None,
                per_day=Decimal("5"),
                per_month=Decimal("10"),
                unknown_cost_policy="allow",
            ),
        )
        day = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
        first = UsageRecord(
            task_id="t1",
            provider_id="openai",
            model_id="gpt-test",
            input_tokens=1_000_000,
            output_tokens=0,
            total_tokens=1_000_000,
            estimated_cost=Decimal("3"),
            currency="USD",
            timestamp=day,
        )
        second = UsageRecord(
            task_id="t2",
            provider_id="openai",
            model_id="gpt-test",
            input_tokens=1_000_000,
            output_tokens=0,
            total_tokens=1_000_000,
            estimated_cost=Decimal("3"),
            currency="USD",
            timestamp=day,
        )
        service.record(first)
        self.assertEqual(service.day_total(day), Decimal("3"))
        self.assertEqual(service.month_total(day), Decimal("3"))
        allowed = service.check_budget(Decimal("1"), when=day)
        self.assertTrue(allowed.allowed)
        service.record(second)
        self.assertEqual(service.day_total(day), Decimal("6"))
        denied = service.check_budget(Decimal("1"), when=day)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "per_day_limit")

    def test_f_explicit_provider_routing_unchanged(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self.assertEqual(set(response.json().keys()), set(CONTRACT_KEYS))

    def test_g_mode_auto_routing_unchanged(self):
        overrides = env_for("openai", "anthropic")
        overrides["AUTO_PROVIDER_ORDER"] = "anthropic,openai"
        overrides["OPENAI_TASK_CATEGORIES"] = "general,technical"
        overrides["ANTHROPIC_TASK_CATEGORIES"] = "general,technical"
        main_mod = load_app(**overrides)
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": "Traceback TypeError app.py",
                    "mode": "auto",
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(response.json()["role"], "Judge")


if __name__ == "__main__":
    unittest.main()
