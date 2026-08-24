import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agents.model_router import (
    REASON_ALL_AVAILABLE_PROVIDERS,
    REASON_EXPLICIT_PROVIDER,
    ModelRouter,
)
from agents.provider_registry import ProviderRecord, ProviderRegistry
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


USER_PROMPT = "Найди поставщика"
OTHER_PROMPT = "Сравни поставщиков"


def registry_with(*available, models=None):
    models = models or {}
    records = {}
    for provider_id in (
        "openai",
        "anthropic",
        "gemini",
        "grok",
        "deepseek",
    ):
        model = models.get(provider_id, f"{provider_id}-model")
        records[provider_id] = ProviderRecord(
            provider_id=provider_id,
            model=model if provider_id in available else model,
            available=provider_id in available,
        )
    return ProviderRegistry(records)


class ModelRouterTests(unittest.TestCase):

    def test_openai_mode_keeps_critic_role(self):
        router = ModelRouter(registry_with("openai", "anthropic"))
        decision = router.decide(mode="openai", role_id="critic")
        self.assertEqual(decision.role_id, "critic")
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)

    def test_anthropic_mode_keeps_strategist_role(self):
        router = ModelRouter(registry_with("openai", "anthropic"))
        decision = router.decide(mode="anthropic", role_id="strategist")
        self.assertEqual(decision.role_id, "strategist")
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)

    def test_both_returns_only_available_providers(self):
        router = ModelRouter(registry_with("openai", "anthropic"))
        decision = router.decide(mode="both", role_id="strategist")
        self.assertEqual(decision.provider_ids, ("openai", "anthropic"))
        self.assertEqual(decision.reason, REASON_ALL_AVAILABLE_PROVIDERS)

    def test_decision_is_deterministic(self):
        router = ModelRouter(registry_with("openai", "anthropic"))
        first = router.decide(mode="openai", role_id="critic")
        second = router.decide(mode="openai", role_id="critic")
        self.assertEqual(first, second)

    def test_decision_does_not_depend_on_prompt_text(self):
        router = ModelRouter(registry_with("openai"))
        first = router.decide(mode="openai", role_id="strategist")
        second = router.decide(mode="openai", role_id="strategist")
        self.assertEqual(first, second)
        self.assertEqual(first.provider_ids, ("openai",))

    def test_models_match_registry_env_snapshot(self):
        registry = registry_with(
            "openai",
            models={"openai": "gpt-test-model"},
        )
        decision = ModelRouter(registry).decide(
            mode="openai",
            role_id="strategist",
        )
        self.assertEqual(decision.models["openai"], "gpt-test-model")

    def test_models_match_process_env_via_from_env(self):
        env = env_for("openai")
        env["OPENAI_MODEL"] = "env-openai-model"
        with patch.dict(os.environ, env, clear=False):
            registry = ProviderRegistry.from_env()
            decision = ModelRouter(registry).decide(
                mode="openai",
                role_id="strategist",
            )
        self.assertEqual(decision.models["openai"], "env-openai-model")

    def test_routing_decision_is_immutable(self):
        decision = ModelRouter(registry_with("openai")).decide(
            mode="openai",
            role_id="strategist",
        )
        with self.assertRaises(Exception):
            decision.role_id = "critic"
        with self.assertRaises(TypeError):
            decision.models["openai"] = "mutated"


class ModelRouterHttpTests(unittest.TestCase):

    def _assert_contract(self, payload):
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(payload["role"], "Judge")

    def test_unavailable_explicit_provider_returns_503_without_fallback(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "gemini",
                    "role": "strategist",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(mocks["anthropic"].await_count, 0)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "provider_not_configured")
        self.assertEqual(detail["provider"], "gemini")

    def test_invalid_mode_returns_http_400(self):
        main_mod = load_app(**env_for("openai"))
        mock_run = AsyncMock(return_value="should not run")
        with patch.object(
            main_mod.router.pipeline.expert_manager.openai,
            "run",
            new=mock_run,
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": USER_PROMPT, "mode": "not-a-provider"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mock_run.await_count, 0)
        self.assertEqual(response.json()["detail"]["error"], "invalid_mode")

    def test_invalid_role_returns_http_400(self):
        main_mod = load_app(**env_for("openai"))
        mock_run = AsyncMock(return_value="should not run")
        with patch.object(
            main_mod.router.pipeline.expert_manager.openai,
            "run",
            new=mock_run,
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "openai",
                    "role": "not-a-role",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mock_run.await_count, 0)
        self.assertEqual(response.json()["detail"]["error"], "invalid_role")

    def test_response_contract_unchanged(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "openai",
                    "role": "critic",
                },
            )
        self.assertEqual(response.status_code, 200)
        self._assert_contract(response.json())
        decision = main_mod.router.last_decision
        self.assertEqual(decision.role_id, "critic")
        self.assertEqual(decision.provider_ids, ("openai",))

    def test_different_prompts_same_routing_decision(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            first = client.post(
                "/api/analyze",
                json={
                    "prompt": USER_PROMPT,
                    "mode": "openai",
                    "role": "critic",
                },
            )
            first_decision = main_mod.router.last_decision
            second = client.post(
                "/api/analyze",
                json={
                    "prompt": OTHER_PROMPT,
                    "mode": "openai",
                    "role": "critic",
                },
            )
            second_decision = main_mod.router.last_decision
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first_decision, second_decision)
        self.assertEqual(mocks["openai"].await_count, 2)


if __name__ == "__main__":
    unittest.main()
