import importlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


PROVIDER_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "XAI_API_KEY",
    "XAI_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "AUTO_PROVIDER_ORDER",
    "AUTO_CAPABILITY_FALLBACK",
    "AUTO_ROUTING_POLICY",
    "OPENAI_TASK_CATEGORIES",
    "ANTHROPIC_TASK_CATEGORIES",
    "GEMINI_TASK_CATEGORIES",
    "XAI_TASK_CATEGORIES",
    "DEEPSEEK_TASK_CATEGORIES",
    "OPENAI_QUALITY_CLASS",
    "ANTHROPIC_QUALITY_CLASS",
    "GEMINI_QUALITY_CLASS",
    "XAI_QUALITY_CLASS",
    "DEEPSEEK_QUALITY_CLASS",
    "OPENAI_COST_CLASS",
    "ANTHROPIC_COST_CLASS",
    "GEMINI_COST_CLASS",
    "XAI_COST_CLASS",
    "DEEPSEEK_COST_CLASS",
    "OPENAI_LATENCY_CLASS",
    "ANTHROPIC_LATENCY_CLASS",
    "GEMINI_LATENCY_CLASS",
    "XAI_LATENCY_CLASS",
    "DEEPSEEK_LATENCY_CLASS",
    "OPENAI_CONTEXT_CLASS",
    "ANTHROPIC_CONTEXT_CLASS",
    "GEMINI_CONTEXT_CLASS",
    "XAI_CONTEXT_CLASS",
    "DEEPSEEK_CONTEXT_CLASS",
    "OPENAI_SUPPORTS_TOOLS",
    "ANTHROPIC_SUPPORTS_TOOLS",
    "GEMINI_SUPPORTS_TOOLS",
    "XAI_SUPPORTS_TOOLS",
    "DEEPSEEK_SUPPORTS_TOOLS",
    "OPENAI_SUPPORTS_VISION",
    "ANTHROPIC_SUPPORTS_VISION",
    "GEMINI_SUPPORTS_VISION",
    "XAI_SUPPORTS_VISION",
    "DEEPSEEK_SUPPORTS_VISION",
    "OPENAI_SUPPORTS_STRUCTURED_OUTPUT",
    "ANTHROPIC_SUPPORTS_STRUCTURED_OUTPUT",
    "GEMINI_SUPPORTS_STRUCTURED_OUTPUT",
    "XAI_SUPPORTS_STRUCTURED_OUTPUT",
    "DEEPSEEK_SUPPORTS_STRUCTURED_OUTPUT",
    "FINOPS_UNKNOWN_COST_POLICY",
    "FINOPS_PER_TASK_LIMIT",
    "FINOPS_PER_DAY_LIMIT",
    "FINOPS_PER_MONTH_LIMIT",
    "OPENAI_INPUT_PRICE_PER_MILLION",
    "OPENAI_OUTPUT_PRICE_PER_MILLION",
    "OPENAI_PRICE_CURRENCY",
    "ANTHROPIC_INPUT_PRICE_PER_MILLION",
    "ANTHROPIC_OUTPUT_PRICE_PER_MILLION",
    "ANTHROPIC_PRICE_CURRENCY",
    "GEMINI_INPUT_PRICE_PER_MILLION",
    "GEMINI_OUTPUT_PRICE_PER_MILLION",
    "GEMINI_PRICE_CURRENCY",
    "XAI_INPUT_PRICE_PER_MILLION",
    "XAI_OUTPUT_PRICE_PER_MILLION",
    "XAI_PRICE_CURRENCY",
    "DEEPSEEK_INPUT_PRICE_PER_MILLION",
    "DEEPSEEK_OUTPUT_PRICE_PER_MILLION",
    "DEEPSEEK_PRICE_CURRENCY",
    "SEARCH_PROVIDER",
    "SEARCH_API_KEY",
    "TOOL_SEARCH_TIMEOUT_SECONDS",
    "FACT_TRUSTED_DOMAINS",
)

CONTRACT_KEYS = (
    "summary",
    "best_solution",
    "analysis",
    "risks",
    "action_plan",
    "confidence",
    "role",
)

HEALTH_PROVIDERS = (
    "openai",
    "anthropic",
    "gemini",
    "grok",
    "deepseek",
)


def _provider_env(**overrides):
    env = {key: "" for key in PROVIDER_ENV_KEYS}
    env.update(overrides)
    return env


def load_app(**overrides):
    env = _provider_env(**overrides)
    with patch.dict(os.environ, env, clear=False):
        with patch("dotenv.load_dotenv", return_value=False):
            import agents.role_registry as role_registry_mod
            importlib.reload(role_registry_mod)
            import agents.router_v2 as router_v2_mod
            importlib.reload(router_v2_mod)
            import main as main_mod
            importlib.reload(main_mod)
            return main_mod


class SmokeTests(unittest.TestCase):

    def test_import_main_without_all_api_keys(self):
        main_mod = load_app()
        self.assertTrue(hasattr(main_mod, "app"))
        self.assertFalse(main_mod.router.has_available_providers())

    def test_health_returns_200_and_all_providers(self):
        main_mod = load_app()
        client = TestClient(main_mod.app)
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            set(payload["providers"].keys()),
            set(HEALTH_PROVIDERS),
        )
        self.assertTrue(
            all(value is False for value in payload["providers"].values())
        )

    def test_analyze_no_providers_returns_structured_error(self):
        main_mod = load_app()
        client = TestClient(main_mod.app)
        response = client.post(
            "/api/analyze",
            json={"prompt": "test", "mode": "both"},
        )
        self.assertEqual(response.status_code, 503)
        detail = response.json()["detail"]
        self.assertEqual(detail["error"], "no_providers_available")

    def test_analyze_passes_v2_pipeline_with_mode(self):
        main_mod = load_app(
            OPENAI_API_KEY="fake-key",
            OPENAI_MODEL="fake-model",
        )
        with patch.object(
            main_mod.router.pipeline.expert_manager.openai,
            "run",
            new=AsyncMock(return_value="successful strategist answer"),
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "analyze this", "mode": "openai"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for key in CONTRACT_KEYS:
            self.assertIn(key, payload)
        self.assertNotIn("Traceback", payload["analysis"])
        self.assertIn(
            "successful strategist answer",
            payload["analysis"],
        )
        memory = main_mod.router.pipeline.decision_memory.history
        self.assertTrue(memory)

    def test_one_provider_exception_does_not_break_request(self):
        main_mod = load_app(
            OPENAI_API_KEY="fake-key",
            OPENAI_MODEL="fake-model",
            ANTHROPIC_API_KEY="fake-key",
            ANTHROPIC_MODEL="fake-model",
        )
        manager = main_mod.router.pipeline.expert_manager
        with patch.object(
            manager.openai,
            "run",
            new=AsyncMock(side_effect=RuntimeError("openai failed")),
        ), patch.object(
            manager.anthropic,
            "run",
            new=AsyncMock(return_value="successful critic answer"),
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "analyze this", "mode": "both"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for key in CONTRACT_KEYS:
            self.assertIn(key, payload)
        self.assertNotIn("Traceback", str(payload))
        self.assertNotIn("openai failed", payload["analysis"])
        self.assertIn("successful critic answer", payload["analysis"])
        self.assertEqual(
            manager.last_errors["openai"]["type"],
            "RuntimeError",
        )


if __name__ == "__main__":
    unittest.main()
