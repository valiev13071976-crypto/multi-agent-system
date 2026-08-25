from dataclasses import asdict, fields
from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import unittest

from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
from agents.provider_result import ProviderResult
from finops.models import BudgetLimits, PriceQuote, UsageRecord
from finops.service import FinOpsService, estimate_cost
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


PROMPT = "Найди поставщика keep this out of usage"
SECRET = "sk-runtime-finops-secret"

QUOTE = PriceQuote(
    provider_id="openai",
    model_id="fake-model",
    input_price_per_million=Decimal("3"),
    output_price_per_million=Decimal("6"),
    currency="USD",
    enabled=True,
)


class FakeAgent:
    def __init__(self, value, model="fake-model"):
        self.value = value
        self.model = model

    async def run(self, prompt):
        return self.value


class RuntimeFinOpsTests(unittest.IsolatedAsyncioTestCase):

    def _assert_contract(self, payload):
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(len(payload.keys()), 7)
        self.assertEqual(payload["role"], "Judge")

    async def test_c_expert_manager_uses_provider_result_text(self):
        result = ProviderResult(
            text="exact expert text",
            provider_id="openai",
            model_id="fake-model",
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
        )
        manager = ExpertManager(openai=FakeAgent(result), finops=FinOpsService())
        experts = await manager.run("user prompt must not be stored")
        self.assertEqual(experts["openai"], "exact expert text")
        self.assertEqual(manager.last_provider_results["openai"].text, "exact expert text")

    async def test_c_expert_manager_accepts_plain_string(self):
        manager = ExpertManager(openai=FakeAgent("legacy string"))
        experts = await manager.run("prompt")
        self.assertEqual(experts["openai"], "legacy string")

    async def test_d_usage_record_does_not_contain_prompt(self):
        result = ProviderResult(
            text="answer",
            provider_id="openai",
            model_id="fake-model",
        )
        service = FinOpsService()
        manager = ExpertManager(openai=FakeAgent(result), finops=service)
        await manager.run(PROMPT)
        record = manager.last_usage[0]
        dumped = str(asdict(record))
        self.assertNotIn(PROMPT, dumped)
        self.assertNotIn("prompt", {field.name for field in fields(UsageRecord)})

    def test_e_known_tokens_and_pricing_are_deterministic(self):
        cost = estimate_cost(QUOTE, 1_000_000, 500_000)
        self.assertEqual(cost, Decimal("6"))
        service = FinOpsService(prices={("openai", "fake-model"): QUOTE})
        self.assertEqual(service.estimate("openai", "fake-model", 1_000_000, 500_000), Decimal("6"))

    async def test_e_runtime_records_exact_cost(self):
        result = ProviderResult(
            text="answer",
            provider_id="openai",
            model_id="fake-model",
            input_tokens=1_000_000,
            output_tokens=500_000,
            total_tokens=1_500_000,
        )
        service = FinOpsService(prices={("openai", "fake-model"): QUOTE})
        manager = ExpertManager(openai=FakeAgent(result), finops=service)
        await manager.run("prompt")
        self.assertEqual(manager.last_usage[0].estimated_cost, Decimal("6"))

    async def test_f_unknown_tokens_mean_unknown_cost(self):
        result = ProviderResult(
            text="answer",
            provider_id="openai",
            model_id="fake-model",
        )
        service = FinOpsService(prices={("openai", "fake-model"): QUOTE})
        manager = ExpertManager(openai=FakeAgent(result), finops=service)
        await manager.run("prompt")
        self.assertIsNone(manager.last_usage[0].estimated_cost)

    async def test_g_unknown_pricing_means_unknown_cost(self):
        result = ProviderResult(
            text="answer",
            provider_id="openai",
            model_id="fake-model",
            input_tokens=10,
            output_tokens=4,
            total_tokens=14,
        )
        service = FinOpsService(prices={})
        manager = ExpertManager(openai=FakeAgent(result), finops=service)
        await manager.run("prompt")
        self.assertIsNone(manager.last_usage[0].estimated_cost)

    async def test_wrong_model_quote_does_not_estimate_cost(self):
        result = ProviderResult(
            text="answer",
            provider_id="openai",
            model_id="model-b",
            input_tokens=1_000_000,
            output_tokens=500_000,
            total_tokens=1_500_000,
        )
        service = FinOpsService(
            prices={
                ("openai", "model-a"): PriceQuote(
                    provider_id="openai",
                    model_id="model-a",
                    input_price_per_million=Decimal("3"),
                    output_price_per_million=Decimal("6"),
                    currency="USD",
                    enabled=True,
                )
            }
        )
        manager = ExpertManager(
            openai=FakeAgent(result, model="model-b"),
            finops=service,
        )
        await manager.run("prompt")
        self.assertIsNone(manager.last_usage[0].estimated_cost)

    def test_unknown_cost_deny_returns_http_429(self):
        secret = "sk-finops-deny-secret"
        overrides = env_for("openai")
        overrides["OPENAI_API_KEY"] = secret
        overrides["FINOPS_UNKNOWN_COST_POLICY"] = "deny"
        main_mod = load_app(**overrides)
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "openai"},
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(mocks["openai"].await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "finops_budget_denied")
        self.assertEqual(
            detail["message"],
            "Request blocked by FinOps budget policy.",
        )
        self.assertEqual(detail["reason"], "unknown_cost_denied")
        self.assertNotIn(secret, response.text)
        self.assertNotIn(PROMPT, response.text)
        self.assertNotIn("Authorization", response.text)

    def test_h_mode_openai_one_usage_record(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        self._assert_contract(response.json())
        records = manager.last_usage
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].provider_id, "openai")
        self.assertEqual(records[0].task_id, main_mod.router.last_task_id)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)

    def test_i_mode_both_two_records_same_task_id(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "both"},
            )
        self.assertEqual(response.status_code, 200)
        self._assert_contract(response.json())
        records = manager.last_usage
        self.assertEqual(len(records), 2)
        self.assertEqual({record.provider_id for record in records}, {"openai", "anthropic"})
        self.assertEqual({record.task_id for record in records}, {main_mod.router.last_task_id})
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)

    def test_j_mode_auto_routing_unchanged(self):
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
        self.assertEqual(len(manager.last_usage), 1)
        self.assertEqual(manager.last_usage[0].provider_id, "anthropic")
        self._assert_contract(response.json())

    def test_k_explicit_provider_semantics_unchanged(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        self.assertEqual(main_mod.router.last_decision.reason, "explicit_provider")

    async def test_l_secrets_are_not_stored(self):
        result = ProviderResult(
            text="answer",
            provider_id="openai",
            model_id="fake-model",
            raw_usage={"input_tokens": 1},
        )
        service = FinOpsService()
        manager = ExpertManager(openai=FakeAgent(result), finops=service)
        await manager.run(PROMPT)
        dumped = repr(result) + str(asdict(manager.last_usage[0]))
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(PROMPT, dumped)

        async def boom(prompt):
            raise RuntimeError(f"Authorization: Bearer {SECRET}")

        failing = FakeAgent("unused")
        failing.run = boom
        manager = ExpertManager(openai=failing, finops=service)
        await manager.run(PROMPT)
        self.assertNotIn(SECRET, manager.last_errors["openai"]["message"])

    async def test_provider_exception_behavior_unchanged(self):
        class Ok:
            model = "fake-model"

            async def run(self, prompt):
                return "ok"

        class Boom:
            model = "fake-model"

            async def run(self, prompt):
                raise RuntimeError("openai failed")

        manager = ExpertManager(openai=Boom(), anthropic=Ok())
        experts = await manager.run("prompt", selected=[("openai", manager.openai), ("anthropic", manager.anthropic)])
        self.assertEqual(experts, {"anthropic": "ok"})
        self.assertEqual(manager.last_errors["openai"]["type"], "RuntimeError")

    async def test_unknown_cost_deny_raises_internal_error(self):
        service = FinOpsService(
            limits=BudgetLimits(
                per_task=None,
                per_day=None,
                per_month=None,
                unknown_cost_policy="deny",
            )
        )
        manager = ExpertManager(openai=FakeAgent("ok"), finops=service)
        with self.assertRaises(FinOpsBudgetDeniedError):
            await manager.run("prompt")

    async def test_post_call_budget_does_not_rollback(self):
        result = ProviderResult(
            text="kept",
            provider_id="openai",
            model_id="fake-model",
            input_tokens=1_000_000,
            output_tokens=0,
            total_tokens=1_000_000,
        )
        service = FinOpsService(
            prices={("openai", "fake-model"): QUOTE},
            limits=BudgetLimits(
                per_task=Decimal("1"),
                per_day=None,
                per_month=None,
                unknown_cost_policy="allow",
            ),
        )
        manager = ExpertManager(openai=FakeAgent(result), finops=service)
        experts = await manager.run("prompt")
        self.assertEqual(experts["openai"], "kept")
        self.assertEqual(manager.last_usage[0].estimated_cost, Decimal("3"))
        self.assertTrue(manager.last_budget_exceeded)

    def test_n_response_contract_unchanged(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        with mock_provider_runs(manager, "openai")[0]:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        self._assert_contract(response.json())
        self.assertNotIn("task_id", response.json())


if __name__ == "__main__":
    unittest.main()
